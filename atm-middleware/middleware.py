"""
middleware.py  —  Layer 2
Runs on port 8000.

Bridge: ATM → Middleware → Core Banking (Spring Boot).

Responsibilities:
  - Forward login / deposit / withdraw to Core Banking
  - Track login lockouts in memory
  - Manage login sessions (Postgres when configured, else in-memory)
  - Hash the confirmed transaction data and log it to Ethereum Sepolia
  - PATCH the canonical hash + blockchainTx back to Core Banking
  - ACK / atomicity timer for withdrawals (in memory)
  - Run blockchain reconciliation worker threads (submit-retry, confirm-poll,
    tamper-check) — all going through Spring Boot's /admin/transactions/*
  - Persist middleware-side operational state (idempotency, sessions,
    transaction audit log, etc.) in its own Postgres, separate from Core Banking's.

Intentionally NOT here:
  - No banking data. Core Banking owns accounts, balances, transactions.
  - No balance calculation. Core Banking returns the new balance.
  - No transaction records. Core Banking stores transactions.

The middleware's own database holds ONLY operational state that the middleware
itself owns end-to-end (idempotency, sessions, transaction_logs; correlation logs /
routing config still planned). It never duplicates banking data.
"""

import inspect
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

if not hasattr(inspect, "getargspec"):
    inspect.getargspec = inspect.getfullargspec

from web3 import Web3

# Make the project-root secrets_manager importable from this subdirectory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

load_dotenv()

import config  # noqa: E402  — keychain first; .env is optional fallback
import blockchain_worker
import db
import idempotency
import sessions
import transaction_logs
from admin_client import AdminClient
from canonical import hash_transaction

# ── Config (from keychain via config.py) ──────────────────────────────────────

CORE_BANKING_URL      = config.CORE_BANKING_URL
SERVICE_TOKEN         = config.SERVICE_TOKEN
ACK_TIMEOUT_SECONDS   = config.ACK_TIMEOUT_SECONDS
SESSION_TTL_SECONDS   = config.SESSION_TTL_SECONDS
sessions.configure(SESSION_TTL_SECONDS)
LOCKOUT_MAX_ATTEMPTS  = config.LOCKOUT_MAX_ATTEMPTS
LOCKOUT_MINUTES       = [5, 10, 15]  # progressive lockout durations

CONTRACT_ADDRESS  = config.CONTRACT_ADDRESS
ETH_PRIVATE_KEY   = config.ETH_PRIVATE_KEY
RPC_URL           = config.RPC_URL
RPC_FALLBACK_URLS = config.RPC_FALLBACK_URLS

CONTRACT_ABI = [
    {
        "inputs": [{"internalType": "string", "name": "_transactionHash", "type": "string"}],
        "name": "storeLog", "outputs": [],
        "stateMutability": "nonpayable", "type": "function",
    },
    {
        "inputs": [{"internalType": "string", "name": "_logHash", "type": "string"}],
        "name": "verifyLog",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view", "type": "function",
    },
]


# ── Admin client (used by deposit/withdraw flow AND background worker) ───────

_admin_client: AdminClient | None = None


def _get_admin_client() -> AdminClient | None:
    global _admin_client
    if _admin_client is None and SERVICE_TOKEN:
        _admin_client = AdminClient(CORE_BANKING_URL, SERVICE_TOKEN)
    return _admin_client


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_atomicity_monitor, daemon=True).start()
    threading.Thread(target=_session_cleanup, daemon=True).start()

    print(f"[Middleware] Layer 2 started on port 8000")
    print(f"[Middleware] Core Banking: {CORE_BANKING_URL}")

    # Initialize the middleware's own DB (idempotency records, etc.). If
    # MIDDLEWARE_DB_URL is unset, persistence is skipped and the middleware
    # runs with the same in-memory-only behaviour it had before.
    try:
        if db.init_db():
            print(f"[Middleware] Operational DB: {db.get_db_url()}")
        else:
            print("[Middleware] Operational DB: disabled (MIDDLEWARE_DB_URL unset)")
    except Exception as e:
        print(f"[Middleware] Operational DB: FAILED to initialize: {e}")
        raise

    print("[Middleware] Banking data lives in Core Banking; middleware DB holds operational state only.")

    # Spawn the blockchain reconciliation worker (submit-retry / confirm-poll /
    # tamper-check) ONLY if everything it needs is configured. Otherwise log and
    # skip — the deposit/withdraw flow still works without it.
    admin = _get_admin_client()
    if admin and CONTRACT_ADDRESS and ETH_PRIVATE_KEY:
        blockchain_worker.start(
            admin=admin,
            submit_to_chain=_submit_to_blockchain,
            get_receipt=_get_chain_receipt,
            verify_on_chain=_verify_log_on_chain,
        )
        print("[Middleware] Blockchain reconciliation worker started.")
    else:
        missing = []
        if not admin:             missing.append("MIDDLEWARE_SERVICE_TOKEN")
        if not CONTRACT_ADDRESS:  missing.append("CONTRACT_ADDRESS")
        if not ETH_PRIVATE_KEY:   missing.append("ETH_PRIVATE_KEY")
        print(f"[Middleware] Worker NOT started — missing: {', '.join(missing)}")

    yield


app = FastAPI(title="ATM Middleware — Layer 2", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def _audit_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Log failed /atm/* requests; re-raise as JSON for the client."""
    if request.url.path.startswith("/atm"):
        detail = exc.detail
        transaction_logs.log_event(
            endpoint=request.url.path,
            http_method=request.method,
            outcome="error",
            response_status_code=exc.status_code,
            response_body={"detail": detail},
            error_message=str(detail),
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _resolve_channel(x_channel: str | None) -> str:
    if x_channel and x_channel.strip():
        return x_channel.strip()
    return transaction_logs.DEFAULT_CHANNEL


def _audit(
    *,
    endpoint: str,
    http_method: str,
    outcome: str,
    status_code: int,
    started: float,
    account_number: str | None = None,
    channel: str = transaction_logs.DEFAULT_CHANNEL,
    idempotency_key: str | None = None,
    request_body: dict | None = None,
    response_body: Any = None,
    error_message: str | None = None,
) -> None:
    transaction_logs.log_event(
        endpoint=endpoint,
        http_method=http_method,
        outcome=outcome,
        response_status_code=status_code,
        account_number=account_number,
        channel=channel,
        idempotency_key=idempotency_key,
        request_body=request_body,
        response_body=response_body,
        duration_ms=int((time.perf_counter() - started) * 1000),
        error_message=error_message,
    )


# ── Blockchain ────────────────────────────────────────────────────────────────

_blockchain      = None
_blockchain_lock = threading.Lock()


def _get_blockchain():
    global _blockchain
    with _blockchain_lock:
        if _blockchain is None and CONTRACT_ADDRESS and ETH_PRIVATE_KEY:
            _blockchain = _init_blockchain()
    return _blockchain


def _init_blockchain():
    for url in [RPC_URL] + RPC_FALLBACK_URLS:
        try:
            sess = requests.Session()
            sess.trust_env = False
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}, session=sess))
            w3.eth.chain_id
            account  = w3.eth.account.from_key(ETH_PRIVATE_KEY)
            contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=CONTRACT_ABI)
            print(f"[Blockchain] Connected to {url}")
            return {"w3": w3, "account": account, "contract": contract}
        except Exception as e:
            print(f"[Blockchain] {url} failed: {e}")
    print("[Blockchain] WARNING: No RPC available — blockchain logging disabled.")
    return None


def _submit_to_blockchain(hash_str: str) -> str | None:
    bc = _get_blockchain()
    if not bc:
        return None
    try:
        w3, account, contract = bc["w3"], bc["account"], bc["contract"]
        latest     = w3.eth.get_block("latest")
        base_fee   = latest.get("baseFeePerGas", w3.eth.gas_price)
        priority   = w3.to_wei(2, "gwei")
        tx = contract.functions.storeLog(hash_str).build_transaction({
            "from":                account.address,
            "nonce":               w3.eth.get_transaction_count(account.address),
            "gas":                 200000,
            "maxFeePerGas":        (2 * int(base_fee)) + int(priority),
            "maxPriorityFeePerGas": priority,
            "type":                2,
            "chainId":             w3.eth.chain_id,
        })
        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        return tx_hash
    except Exception as e:
        print(f"[Blockchain] submit failed: {e}")
        return None


def _get_chain_receipt(tx_hash: str) -> dict | None:
    """Return a normalized receipt dict, or None if the tx isn't mined yet."""
    bc = _get_blockchain()
    if not bc:
        return None
    try:
        receipt = bc["w3"].eth.get_transaction_receipt(tx_hash)
    except Exception:
        return None  # not yet mined / RPC hiccup
    if receipt is None:
        return None
    return {"status": int(receipt.get("status", 0))}


def _verify_log_on_chain(canonical_hash: str) -> bool:
    """Call contract.verifyLog(hash) → True if the hash is stored on chain."""
    bc = _get_blockchain()
    if not bc:
        return False
    try:
        return bool(bc["contract"].functions.verifyLog(canonical_hash).call())
    except Exception:
        return False


def _session_cleanup() -> None:
    """Evict sessions idle longer than SESSION_TTL_SECONDS. Runs every 60s."""
    while True:
        time.sleep(60)
        n = sessions.cleanup_expired()
        if n:
            print(f"[Sessions] Evicted {n} idle session(s)")


# ── In-memory lockout tracker ─────────────────────────────────────────────────
# Maps account_number → {failed_attempts, locked_until (epoch float or None)}

_lockouts:      dict[str, dict] = {}
_lockouts_lock: threading.Lock  = threading.Lock()


def _check_lockout(account_number: str) -> dict | None:
    """Returns lockout info dict if locked, None if free to attempt."""
    with _lockouts_lock:
        entry = _lockouts.get(account_number)
        if not entry:
            return None
        locked_until = entry.get("locked_until")
        if locked_until and time.time() < locked_until:
            remaining = int(locked_until - time.time())
            return {"remaining_lock_seconds": remaining, "lock_minutes": remaining // 60}
        # Lock expired — clear it
        if locked_until and time.time() >= locked_until:
            entry["locked_until"]    = None
            entry["failed_attempts"] = 0
        return None


def _record_failed_attempt(account_number: str) -> dict:
    """Increment failure counter, apply progressive lockout. Returns response dict."""
    with _lockouts_lock:
        entry = _lockouts.setdefault(account_number, {"failed_attempts": 0, "locked_until": None})
        entry["failed_attempts"] += 1
        failed = entry["failed_attempts"]

        if failed % LOCKOUT_MAX_ATTEMPTS == 0:
            level        = min(failed // LOCKOUT_MAX_ATTEMPTS, len(LOCKOUT_MINUTES)) - 1
            lock_minutes = LOCKOUT_MINUTES[level]
            entry["locked_until"] = time.time() + (lock_minutes * 60)
            return {"status": "locked",
                    "remaining_lock_seconds": lock_minutes * 60,
                    "lock_minutes": lock_minutes}

        attempts_to_next = LOCKOUT_MAX_ATTEMPTS - (failed % LOCKOUT_MAX_ATTEMPTS)
        return {"status": "invalid", "attempts_to_next_lock": attempts_to_next}


def _reset_lockout(account_number: str) -> None:
    with _lockouts_lock:
        _lockouts.pop(account_number, None)


# ── In-memory ACK tracker (withdraw atomicity) ────────────────────────────────
# Maps middleware_tx_id → {account_id, amount, jwt, deadline}

_pending_acks: dict[int, dict] = {}
_ack_counter  = 0
_ack_lock     = threading.Lock()


def _new_ack_id() -> int:
    global _ack_counter
    with _ack_lock:
        _ack_counter += 1
        return _ack_counter


def _atomicity_monitor() -> None:
    """Roll back any withdrawal that doesn't get an ACK within the timeout."""
    while True:
        now     = time.time()
        expired = [(tid, d) for tid, d in list(_pending_acks.items()) if now > d["deadline"]]
        for tx_id, data in expired:
            _pending_acks.pop(tx_id, None)
            print(f"[Atomicity] No ACK for tx #{tx_id} — reversing ${data['amount']}")
            try:
                _cb_post(f"/accounts/{data['account_id']}/deposit",
                         {"amountDeposit": data["amount"]}, data["jwt"])
            except Exception as e:
                print(f"[Atomicity] Reversal failed for tx #{tx_id}: {e}")
        time.sleep(1)


# ── Core Banking HTTP client ──────────────────────────────────────────────────

def _cb_post(path: str, body: dict, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(f"{CORE_BANKING_URL}{path}", json=body,
                             headers=headers, timeout=(3, 12))
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, f"Cannot reach Core Banking at {CORE_BANKING_URL}")
    return resp


# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    accountNumber: str
    pin: str

class AmountRequest(BaseModel):
    amount: float

class AckRequest(BaseModel):
    middlewareTxId: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "layer": 2, "service": "ATM Middleware"}


@app.post("/atm/login")
def atm_login(
    req: LoginRequest,
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    req_audit = {"accountNumber": req.accountNumber, "pin": "***REDACTED***"}

    lockout = _check_lockout(req.accountNumber)
    if lockout:
        body = {"status": "locked", **lockout}
        _audit(
            endpoint="/atm/login", http_method="POST", outcome="success",
            status_code=200, started=started, account_number=req.accountNumber,
            channel=channel, request_body=req_audit, response_body=body,
        )
        return body

    resp = _cb_post("/atm/login", {"accountNumber": req.accountNumber, "pin": req.pin})

    if resp.status_code == 401:
        result = _record_failed_attempt(req.accountNumber)
        _audit(
            endpoint="/atm/login", http_method="POST", outcome="success",
            status_code=200, started=started, account_number=req.accountNumber,
            channel=channel, request_body=req_audit, response_body=result,
        )
        return result

    if resp.status_code == 403:
        body = {"status": "locked", "remaining_lock_seconds": 0, "lock_minutes": 0}
        _audit(
            endpoint="/atm/login", http_method="POST", outcome="success",
            status_code=200, started=started, account_number=req.accountNumber,
            channel=channel, request_body=req_audit, response_body=body,
        )
        return body

    if not resp.ok:
        raise HTTPException(502, f"Core Banking error: {resp.text}")

    data = resp.json()
    _reset_lockout(req.accountNumber)

    session_token = sessions.create(
        jwt=data["token"],
        account_id=int(data["accountId"]),
        account_number=req.accountNumber,
        balance=float(data.get("balance", 0)),
        customer_name=data.get("customerName", "Customer"),
    )

    body = {
        "status":        "ok",
        "sessionToken":  session_token,
        "customerName":  data.get("customerName", "Customer"),
        "accountNumber": req.accountNumber,
        "balance":       float(data.get("balance", 0)),
        "account": {
            "account_id": req.accountNumber,
            "name":       data.get("customerName", "Customer"),
            "balance":    float(data.get("balance", 0)),
        },
    }
    safe_body = {**body, "sessionToken": "***REDACTED***"}
    _audit(
        endpoint="/atm/login", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=req.accountNumber,
        channel=channel, request_body=req_audit, response_body=safe_body,
    )
    return body


@app.post("/atm/logout")
def atm_logout(
    x_session_token: str = Header(...),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    account_number = None
    try:
        account_number = sessions.get(x_session_token)["account_number"]
    except HTTPException:
        pass
    sessions.remove(x_session_token)
    body = {"status": "logged_out"}
    _audit(
        endpoint="/atm/logout", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, response_body=body,
    )
    return body


@app.post("/atm/session/continue")
def atm_session_continue(
    x_session_token: str = Header(...),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    """Customer chose another transaction — extend the idle session window."""
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    if not sessions.touch(x_session_token):
        raise HTTPException(401, "Invalid or expired session. Please log in again.")
    body = {"status": "ok"}
    _audit(
        endpoint="/atm/session/continue", http_method="POST", outcome="success",
        status_code=200, started=started, channel=channel, response_body=body,
    )
    return body


def _hash_and_persist(transaction_id: int,
                      account_number: str,
                      transaction_type: str,
                      amount: float,
                      balance_after: float,
                      reference_id: str,
                      created_at: str) -> tuple[str, str | None]:
    """
    Compute the canonical hash, submit to chain, and PATCH the row in Core
    Banking so the worker has durable bookkeeping. On any failure the row
    stays at chainStatus=PENDING_SUBMIT and the worker will retry it later.
    Returns (canonical_hash, blockchain_tx_or_None).
    """
    c_hash = hash_transaction(
        account_number=account_number,
        transaction_type=transaction_type,
        amount=amount,
        balance_after=balance_after,
        reference_id=reference_id,
        created_at=created_at,
    )
    try:
        bc_tx = _submit_to_blockchain(c_hash)
    except Exception as e:
        bc_tx = None
        print(f"[Middleware] inline submit failed for tx {transaction_id}: {e}")

    admin = _get_admin_client()
    if admin:
        try:
            admin.patch_blockchain(
                transaction_id=transaction_id,
                canonical_hash=c_hash,
                blockchain_tx=bc_tx,
                submit_error=None if bc_tx else "inline submit failed",
            )
        except Exception as e:
            print(f"[Middleware] PATCH /admin/transactions/{transaction_id}/blockchain failed: {e}")
    return c_hash, bc_tx


@app.post("/atm/deposit")
def atm_deposit(
    req: AmountRequest,
    x_session_token: str = Header(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    session       = sessions.get(x_session_token)
    account_id    = session["account_id"]
    account_number = session["account_number"]
    old_balance   = session["balance"]
    jwt           = session["jwt"]

    cached = idempotency.begin(
        idempotency_key, account_number, "/atm/deposit", req.model_dump()
    )
    if cached is not None:
        _audit(
            endpoint="/atm/deposit", http_method="POST", outcome="cached",
            status_code=200, started=started, account_number=account_number,
            channel=channel, idempotency_key=idempotency_key,
            request_body=req.model_dump(), response_body=cached,
        )
        return cached

    # 1. Forward to Core Banking — it validates, updates balance, saves transaction
    resp = _cb_post(f"/accounts/{account_id}/deposit", {"amountDeposit": req.amount}, jwt)
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)

    # 2. Read authoritative result — Core Banking did all the math
    result      = resp.json()
    tx_id       = int(result["transactionId"])
    new_balance = float(result.get("balanceAfter", 0))
    ref_id      = str(result.get("referenceId", ""))
    created_at  = str(result.get("createdAt", datetime.now(timezone.utc).isoformat()))
    sessions.update_balance(x_session_token, new_balance)

    # 3. Hash confirmed data, log to chain, persist hash+tx into Core Banking
    c_hash, bc_tx = _hash_and_persist(
        transaction_id=tx_id,
        account_number=account_number,
        transaction_type="DEPOSIT",
        amount=req.amount,
        balance_after=new_balance,
        reference_id=ref_id,
        created_at=created_at,
    )

    response = {
        "status":        "SUCCESS",
        "amount":        req.amount,
        "oldBalance":    old_balance,
        "newBalance":    new_balance,
        "canonicalHash": c_hash,
        "blockchainTx":  bc_tx,
        "verifyUrl":     f"https://sepolia.etherscan.io/tx/{bc_tx}" if bc_tx else None,
        "referenceId":   ref_id,
        "transactionId": tx_id,
        "message":       "" if bc_tx else "Blockchain sync unavailable — worker will retry.",
    }

    # 4. Cache the response so a retry of the same Idempotency-Key returns
    #    the exact same payload without re-charging the account.
    idempotency.finish(idempotency_key, account_number, response)

    _audit(
        endpoint="/atm/deposit", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, idempotency_key=idempotency_key,
        request_body=req.model_dump(), response_body=response,
    )
    return response


@app.post("/atm/withdraw")
def atm_withdraw(
    req: AmountRequest,
    x_session_token: str = Header(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    session        = sessions.get(x_session_token)
    account_id     = session["account_id"]
    account_number = session["account_number"]
    old_balance    = session["balance"]
    jwt            = session["jwt"]

    cached = idempotency.begin(
        idempotency_key, account_number, "/atm/withdraw", req.model_dump()
    )
    if cached is not None:
        _audit(
            endpoint="/atm/withdraw", http_method="POST", outcome="cached",
            status_code=200, started=started, account_number=account_number,
            channel=channel, idempotency_key=idempotency_key,
            request_body=req.model_dump(), response_body=cached,
        )
        return cached

    # 1. Forward to Core Banking — it validates funds, subtracts balance, saves transaction
    resp = _cb_post(f"/accounts/{account_id}/withdraw", {"amountWithdraw": req.amount}, jwt)
    if resp.status_code == 400:
        raise HTTPException(400, "Insufficient funds")
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)

    # 2. Read authoritative result — Core Banking did all the math
    result      = resp.json()
    tx_id       = int(result["transactionId"])
    new_balance = float(result.get("balanceAfter", 0))
    ref_id      = str(result.get("referenceId", ""))
    created_at  = str(result.get("createdAt", datetime.now(timezone.utc).isoformat()))
    sessions.update_balance(x_session_token, new_balance)

    # 3. Hash confirmed data, log to chain, persist hash+tx into Core Banking
    c_hash, bc_tx = _hash_and_persist(
        transaction_id=tx_id,
        account_number=account_number,
        transaction_type="WITHDRAW",
        amount=req.amount,
        balance_after=new_balance,
        reference_id=ref_id,
        created_at=created_at,
    )

    # 4. Register ACK timer — if ATM doesn't confirm cash dispensed within
    #    ACK_TIMEOUT_SECONDS, atomicity monitor re-deposits the amount
    ack_id = _new_ack_id()
    _pending_acks[ack_id] = {
        "account_id": account_id,
        "amount":     req.amount,
        "jwt":        jwt,
        "deadline":   time.time() + ACK_TIMEOUT_SECONDS,
    }

    response = {
        "middlewareTxId": ack_id,
        "transactionId":  tx_id,
        "status":         "SUCCESS",
        "amount":         req.amount,
        "oldBalance":     old_balance,
        "newBalance":     new_balance,
        "canonicalHash":  c_hash,
        "blockchainTx":   bc_tx,
        "verifyUrl":      f"https://sepolia.etherscan.io/tx/{bc_tx}" if bc_tx else None,
        "referenceId":    ref_id,
        "message":        "Dispense cash now, then call /atm/ack",
    }

    # 5. Cache the response — retries with the same Idempotency-Key get the
    #    same middlewareTxId / transactionId without a second debit.
    idempotency.finish(idempotency_key, account_number, response)

    _audit(
        endpoint="/atm/withdraw", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, idempotency_key=idempotency_key,
        request_body=req.model_dump(), response_body=response,
    )
    return response


@app.post("/atm/ack")
def atm_ack(
    req: AckRequest,
    x_session_token: str = Header(...),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    """ATM calls this after physically dispensing cash — cancels the rollback timer."""
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    session = sessions.get(x_session_token)
    _pending_acks.pop(req.middlewareTxId, None)
    body = {"status": "CONFIRMED", "middlewareTxId": req.middlewareTxId}
    _audit(
        endpoint="/atm/ack", http_method="POST", outcome="success",
        status_code=200, started=started,
        account_number=session["account_number"], channel=channel,
        request_body=req.model_dump(), response_body=body,
    )
    return body


@app.get("/atm/balance")
def get_balance(
    x_session_token: str = Header(...),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    """Returns the cached balance. For a live balance, call Core Banking directly."""
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    session = sessions.get(x_session_token)
    body = {"balance": session["balance"], "accountNumber": session["account_number"]}
    _audit(
        endpoint="/atm/balance", http_method="GET", outcome="success",
        status_code=200, started=started,
        account_number=session["account_number"], channel=channel,
        response_body=body,
    )
    return body


@app.get("/atm/transactions")
def get_transactions(
    x_session_token: str = Header(...),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    """Proxy to Core Banking transaction history."""
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    session    = sessions.get(x_session_token)
    account_id = session["account_id"]
    jwt        = session["jwt"]
    try:
        resp = requests.get(
            f"{CORE_BANKING_URL}/accounts/{account_id}/transactions",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=(3, 12),
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, "Cannot reach Core Banking")
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)
    body = resp.json()
    _audit(
        endpoint="/atm/transactions", http_method="GET", outcome="success",
        status_code=200, started=started,
        account_number=session["account_number"], channel=channel,
        response_body=body,
    )
    return body


@app.get("/atm/tx-status/{transaction_id}")
def atm_tx_status(
    transaction_id: int,
    x_session_token: str = Header(...),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    session = sessions.get(x_session_token)
    try:
        resp = requests.get(
            f"{CORE_BANKING_URL}/admin/transactions/{transaction_id}",
            headers={"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"},
            timeout=(3, 10),
        )
        if not resp.ok:
            raise HTTPException(resp.status_code, resp.text)
        body = resp.json()
        _audit(
            endpoint=f"/atm/tx-status/{transaction_id}", http_method="GET",
            outcome="success", status_code=200, started=started,
            account_number=session["account_number"], channel=channel,
            response_body=body,
        )
        return body
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, "Cannot reach Core Banking")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("middleware:app", host="0.0.0.0", port=8000, reload=False)