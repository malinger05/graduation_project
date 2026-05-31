"""
middleware.py  —  Layer 2
Runs on port 8000.

Bridge: ATM → Middleware → Core Banking (Spring Boot).

Responsibilities:
  - Forward login / deposit / withdraw to Core Banking
  - Track login lockouts (Postgres when configured, else in-memory)
  - Manage login sessions (Postgres when configured, else in-memory)
  - Hash the confirmed transaction data and log it to Ethereum Sepolia
  - PATCH the canonical hash + blockchainTx back to Core Banking
  - Forward withdraw dispense ACK to Core Banking (state owned there)
  - Run blockchain reconciliation worker threads (submit-retry, confirm-poll,
    tamper-check) — all going through Spring Boot's /admin/transactions/*
  - Persist middleware-side operational state (idempotency, sessions,
    transaction audit log, correlation traces, etc.) in its own Postgres, separate
    from Core Banking's.

Intentionally NOT here:
  - No banking data. Core Banking owns accounts, balances, transactions.
  - No balance calculation. Core Banking returns the new balance.
  - No transaction records. Core Banking stores transactions.

The middleware's own database holds ONLY operational state that the middleware
itself owns end-to-end (idempotency, sessions, login_lockouts, transaction_logs,
correlation_logs; routing_config still planned). It never duplicates banking data.
"""

import inspect
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import cb_http
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
import correlation
import idempotency
import lockouts
import sessions
import retention
import transaction_logs
import client_cert
from admin_client import AdminClient
from canonical import hash_transaction

# ── Config (from keychain via config.py) ──────────────────────────────────────

CORE_BANKING_URL      = config.CORE_BANKING_URL
SERVICE_TOKEN         = config.SERVICE_TOKEN
ACK_TIMEOUT_SECONDS   = config.ACK_TIMEOUT_SECONDS
SESSION_TTL_SECONDS   = config.SESSION_TTL_SECONDS
sessions.configure(SESSION_TTL_SECONDS)
# Always lock after 3 consecutive failures per block (policy is tiered minutes, not attempt count).
LOCKOUT_BLOCK_ATTEMPTS = 3
LOCKOUT_MINUTES        = config.LOCKOUT_MINUTES
if config.LOCKOUT_MAX_ATTEMPTS != LOCKOUT_BLOCK_ATTEMPTS:
    print(
        f"[Lockouts] LOCKOUT_MAX_ATTEMPTS={config.LOCKOUT_MAX_ATTEMPTS} ignored; "
        f"using block size {LOCKOUT_BLOCK_ATTEMPTS}"
    )
lockouts.configure(LOCKOUT_BLOCK_ATTEMPTS, LOCKOUT_MINUTES)
if max(LOCKOUT_MINUTES) < 1:
    print(f"[Lockouts] FAST TEST mode — tier minutes: {LOCKOUT_MINUTES}")

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

_cert_monitor_stop = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_session_cleanup, daemon=True).start()
    threading.Thread(target=_retention_cleanup, daemon=True).start()
    threading.Thread(
        target=client_cert.monitor_loop,
        args=(_cert_monitor_stop,),
        daemon=True,
    ).start()

    print(f"[Middleware] Layer 2 started on port 8000")
    print(f"[Middleware] Core Banking: {CORE_BANKING_URL}")

    try:
        db_ready = db.init_db()
        if not db_ready:
            if config.MIDDLEWARE_REQUIRE_DB:
                raise RuntimeError(
                    "MIDDLEWARE_DB_URL is required but not set. "
                    "Run: python3 scripts/manage_secrets.py set MIDDLEWARE_DB_URL "
                    "(see .env.example). For local experiments only: MIDDLEWARE_REQUIRE_DB=0"
                )
            print("[Middleware] Operational DB: disabled (MIDDLEWARE_DB_URL unset)")
        elif db_ready:
            print(f"[Middleware] Operational DB: {db.get_db_url()}")
            if config.TRANSACTION_LOG_RETENTION_DAYS > 0:
                print(
                    f"[Middleware] Retention: transaction_logs older than "
                    f"{config.TRANSACTION_LOG_RETENTION_DAYS} days purged every "
                    f"{config.RETENTION_CLEANUP_INTERVAL_SECONDS}s"
                )
            else:
                print("[Middleware] Retention: disabled (TRANSACTION_LOG_RETENTION_DAYS=0)")
            if config.CLIENT_CERT_MONITOR_ENABLED:
                allowed = client_cert.load_allowed_serials()
                enforce = "enforce allow-list (403)" if config.CLIENT_CERT_ENFORCE_ALLOWLIST else "log only"
                print(
                    f"[Middleware] Client cert monitor: {len(allowed)} allowed serial(s), "
                    f"{enforce}, scan every {config.CLIENT_CERT_MONITOR_INTERVAL_SECONDS}s"
                )
    except Exception as e:
        print(f"[Middleware] Operational DB: FAILED to initialize: {e}")
        raise

    print("[Middleware] Banking data lives in Core Banking; middleware DB holds operational state only.")

    # Always run reconciliation worker threads in the background (submit-retry,
    # confirm-poll, tamper-check). Each loop resolves config on every tick so
    # banking still works when secrets are missing; chain jobs retry once ready.
    blockchain_worker.start(
        get_admin=_get_admin_client,
        submit_to_chain=_submit_to_blockchain,
        get_receipt=_get_chain_receipt,
        verify_on_chain=_verify_log_on_chain,
    )
    print("[Middleware] Blockchain reconciliation worker threads started (background).")
    missing = []
    if not SERVICE_TOKEN:
        missing.append("MIDDLEWARE_SERVICE_TOKEN")
    if not CONTRACT_ADDRESS:
        missing.append("CONTRACT_ADDRESS")
    if not ETH_PRIVATE_KEY:
        missing.append("ETH_PRIVATE_KEY")
    if missing:
        print(
            f"[Middleware] Worker running but chain reconciliation inactive until "
            f"configured: {', '.join(missing)}"
        )

    yield

    _cert_monitor_stop.set()


app = FastAPI(title="ATM Middleware — Layer 2", lifespan=lifespan)
@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    if request.headers.get("content-length"):
        if int(request.headers["content-length"]) > 1_048_576:
            return JSONResponse(status_code=413, content={"detail": "Request too large"})
    return await call_next(request)

@app.middleware("http")
async def _capture_client_cert(request: Request, call_next):
    info = client_cert.extract_from_headers(dict(request.headers))
    client_cert.set_current(info)
    try:
        if request.url.path.startswith("/atm"):
            blocked = client_cert.rejection_detail(info, endpoint=request.url.path)
            if blocked:
                fields = client_cert.format_cert_audit_fields(info)
                transaction_logs.log_event(
                    endpoint=request.url.path,
                    http_method=request.method,
                    outcome="error",
                    response_status_code=403,
                    response_body={"detail": blocked},
                    error_message=blocked,
                    **fields,
                )
                return JSONResponse(status_code=403, content={"detail": blocked})
        return await call_next(request)
    finally:
        client_cert.set_current(None)


@app.exception_handler(HTTPException)
async def _audit_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Log failed /atm/* requests; re-raise as JSON for the client."""
    if request.url.path.startswith("/atm"):
        detail = exc.detail
        cert = client_cert.extract_from_headers(dict(request.headers))
        cert_warn = client_cert.check_and_warn(cert, endpoint=request.url.path)
        fields = client_cert.format_cert_audit_fields(cert)
        err = str(detail)
        if cert_warn:
            err = f"{err}; {cert_warn}" if err else cert_warn
        transaction_logs.log_event(
            endpoint=request.url.path,
            http_method=request.method,
            outcome="error",
            response_status_code=exc.status_code,
            response_body={"detail": detail},
            error_message=err,
            **fields,
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _resolve_channel(x_channel: str | None) -> str:
    if x_channel and x_channel.strip():
        return x_channel.strip()
    return transaction_logs.DEFAULT_CHANNEL


def _require_idempotency_key(idempotency_key: str | None) -> str:
    """Reject deposit/withdraw requests without a non-empty Idempotency-Key header."""
    key = (idempotency_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required for deposit and withdraw.",
        )
    return key


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
    correlation_id: str | None = None,
) -> None:
    cert = client_cert.current()
    cert_warn = client_cert.check_and_warn(cert, endpoint=endpoint)
    fields = client_cert.format_cert_audit_fields(cert)
    err = error_message
    if cert_warn:
        err = f"{err}; {cert_warn}" if err else cert_warn
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
        error_message=err,
        correlation_id=correlation_id,
        **fields,
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
        return None
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
    """Evict idle sessions and clear expired login lockouts. Runs every 60s."""
    while True:
        time.sleep(60)
        n = sessions.cleanup_expired()
        if n:
            print(f"[Sessions] Evicted {n} idle session(s)")
        cleared = lockouts.cleanup_expired()
        if cleared:
            print(f"[Lockouts] Cleared {cleared} expired lockout(s)")


def _retention_cleanup() -> None:
    """Purge old logs, expired idempotency rows, and permanently-locked accounts on a schedule."""
    interval        = config.RETENTION_CLEANUP_INTERVAL_SECONDS
    days            = config.TRANSACTION_LOG_RETENTION_DAYS
    locked_days     = int(os.environ.get("PERMANENTLY_LOCKED_ACCOUNT_CLEANUP_DAYS", "60"))

    _admin_jwt_cache: list[str] = []

    def _get_admin_jwt() -> str | None:
        if _admin_jwt_cache:
            return _admin_jwt_cache[0]
        admin_user = os.environ.get("ADMIN_PANEL_USERNAME", "admin")
        admin_pass = os.environ.get("ADMIN_PANEL_PASSWORD", "admin123")
        try:
            resp = cb_http.post(
                f"{CORE_BANKING_URL}/auth/login",
                json={"username": admin_user, "password": admin_pass},
                timeout=(3, 10),
            )
            if resp.ok:
                jwt = resp.json().get("token")
                if jwt:
                    _admin_jwt_cache.append(jwt)
                    return jwt
        except Exception:
            pass
        return None

    while True:
        time.sleep(interval)
        if not db.is_enabled():
            continue
        try:
            jwt = _get_admin_jwt()
            counts = retention.run_retention(
                transaction_log_retention_days=days,
                core_banking_url=CORE_BANKING_URL,
                admin_jwt=jwt,
                permanently_locked_cleanup_days=locked_days,
            )
            removed = {k: v for k, v in counts.items() if v > 0}
            if removed:
                print(f"[Retention] Purged rows: {removed}")
        except Exception as e:
            print(f"[Retention] Cleanup failed: {e}")


# ── Core Banking HTTP client ──────────────────────────────────────────────────

def _cb_post(path: str, body: dict, token: str | None = None, extra_headers: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    try:
        resp = cb_http.post(f"{CORE_BANKING_URL}{path}", json=body,
                            headers=headers, timeout=(3, 12))
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, f"Cannot reach Core Banking at {CORE_BANKING_URL}")
    return resp


def _cb_post_service(path: str, body: dict):
    """Core Banking call authenticated with middleware service token."""
    if not SERVICE_TOKEN:
        raise HTTPException(503, "MIDDLEWARE_SERVICE_TOKEN not configured")
    return _cb_post(path, body, extra_headers={"X-Service-Token": SERVICE_TOKEN})


def _resolve_card_to_account(card_number: str) -> str | None:
    """
    Call GET /atm/resolve-card?cardNumber=... on Core Banking.

    Returns the accountNumber string on success, or None if the card is not
    found / cancelled.  Does NOT raise — callers decide how to handle a missing
    card (return a generic auth error so as not to leak card validity).
    """
    try:
        resp = cb_http.get(
            f"{CORE_BANKING_URL}/atm/resolve-card",
            params={"cardNumber": card_number},
            timeout=(3, 10),
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, f"Cannot reach Core Banking at {CORE_BANKING_URL}")

    if resp.status_code == 200:
        return resp.json().get("accountNumber")
    # 404 (card not found) or 403 (cancelled) — treat as unknown card
    return None


# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    cardNumber: str
    pin: str

class AmountRequest(BaseModel):
    amount: float

class AckRequest(BaseModel):
    middlewareTxId: int


class AccountStatusRequest(BaseModel):
    # FIX: Support both cardNumber (from ATM) and accountNumber (from admin panel).
    # Exactly one must be provided.
    cardNumber: str | None = None
    accountNumber: str | None = None


class AdminUnlockRequest(BaseModel):
    accountNumber: str   # Admin panel unlocks by account number


class ResetPinRequest(BaseModel):
    cardNumber: str
    newPin: str
    confirmPin: str


class CreateCardRequest(BaseModel):
    accountNumber: str
 
 
class SetOwnPinRequest(BaseModel):
    cardId: int
    accountNumber: str
    pin: str
    pinConfirm: str


class PrepareOwnPinRequest(BaseModel):
    accountNumber: str
    cardNumber: str | None = None


class RegisterFingerprintRequest(BaseModel):
    accountNumber: str
    fingerprintSlotId: int


def _require_service_token(x_service_token: str | None) -> None:
    if not SERVICE_TOKEN or (x_service_token or "").strip() != SERVICE_TOKEN:
        raise HTTPException(401, "Unauthorized")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "layer": 2, "service": "ATM Middleware"}


@app.get("/health/cert-headers")
def health_cert_headers(request: Request):
    """Diagnostic: client cert metadata received from Caddy (call via https://mw.local with mTLS)."""
    headers = dict(request.headers)
    info = client_cert.extract_from_headers(headers)
    der_key = next((k for k in headers if k.lower() == "x-client-cert-der"), None)
    der_len = len(headers[der_key]) if der_key else 0
    return {
        "client_cert_subject": info.subject,
        "client_cert_serial": info.normalized_serial,
        "der_header_length": der_len,
        "cert_related_headers": sorted(
            k for k in headers if "cert" in k.lower() or "client" in k.lower()
        ),
    }


@app.post("/atm/account-status")
def atm_account_status(
    req: AccountStatusRequest,
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    """
    Lockout / PIN-reset state for a card or account (no PIN required).

    ATM callers send cardNumber — middleware resolves it to accountNumber first
    so the lockout counter is always keyed by accountNumber.

    Admin panel callers may send accountNumber directly (they already know it
    and do not have a card number to pass).

    BUG FIX: The original model only accepted cardNumber. The admin_app was
    passing accountNumber, which caused the card resolution to fail silently
    and always return {"status": "ok"} — meaning the lockout state was invisible
    to the admin panel.
    """
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    corr = correlation.new_correlation_id()

    # Resolve to account_number — accept either field.
    if req.cardNumber:
        req_audit = {"cardNumber": req.cardNumber}
        account_number = _resolve_card_to_account(req.cardNumber)
        if account_number is None:
            # Unknown or cancelled card — return ok so the ATM shows a generic
            # error only after PIN entry (avoids leaking which card numbers are valid).
            body = {"status": "ok", "accountNumber": None}
            _audit(
                endpoint="/atm/account-status", http_method="POST", outcome="success",
                status_code=200, started=started, account_number=None,
                channel=channel, request_body=req_audit, response_body=body,
                correlation_id=corr,
            )
            return body
    elif req.accountNumber:
        req_audit = {"accountNumber": req.accountNumber}
        account_number = req.accountNumber
    else:
        raise HTTPException(400, "Provide either cardNumber or accountNumber.")

    lockout = lockouts.check(account_number)

    # BUG FIX: The old code built {"status": "locked", **lockout} when lockout
    # already contained a "status" key, creating a duplicate. Now we return the
    # lockout dict directly (it already has "status") and only add accountNumber
    # on the success path.
    if lockout:
        body = lockout  # lockout dict already contains "status"
    else:
        body = {"status": "ok", "accountNumber": account_number}

    _audit(
        endpoint="/atm/account-status", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, request_body=req_audit, response_body=body,
        correlation_id=corr,
    )
    return body


@app.post("/atm/login")
def atm_login(
    req: LoginRequest,
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    req_audit = {"cardNumber": req.cardNumber, "pin": "***REDACTED***"}
    corr = correlation.new_correlation_id()
    correlation.log_step(
        corr, "request_received", "middleware", "ok",
        account_number=None, endpoint="/atm/login",
    )

    # ── Step 1: resolve card number → account number ──────────────────────────
    account_number = _resolve_card_to_account(req.cardNumber)

    # ── Step 2: lockout check (keyed by accountNumber) ────────────────────────
    if account_number is not None:
        lockout = lockouts.check(account_number)
        if lockout:
            # lockout dict already contains "status" — return it directly
            body = lockout
            step_msg = "pin reset required" if body.get("status") == "pin_reset_required" else "account locked"
            correlation.log_step(
                corr, "lockout_check", "middleware", "skipped",
                account_number=account_number, endpoint="/atm/login",
                message=step_msg,
            )
            _audit(
                endpoint="/atm/login", http_method="POST", outcome="success",
                status_code=200, started=started, account_number=account_number,
                channel=channel, request_body=req_audit, response_body=body,
                correlation_id=corr,
            )
            return body

    correlation.log_step(
        corr, "core_banking_request", "core_banking", "ok",
        account_number=account_number, endpoint="/atm/login",
    )

    # ── Step 3: forward to Core Banking ──────────────────────────────────────
    resp = _cb_post("/atm/login", {"cardNumber": req.cardNumber, "pin": req.pin})

    if resp.status_code == 401:
        # Wrong PIN — record failure against accountNumber if we have it.
        if account_number is not None:
            try:
                result = lockouts.record_failure(account_number)
            except Exception as e:
                print(f"[Lockouts] record_failure failed for account {account_number}: {e}")
                raise HTTPException(500, f"Lockout state error: {e}") from e
        else:
            result = {"status": "invalid", "attempts_to_next_lock": 3}
        correlation.log_step(
            corr, "core_banking_response", "core_banking", "error",
            account_number=account_number, endpoint="/atm/login",
            message="invalid credentials",
        )
        _audit(
            endpoint="/atm/login", http_method="POST", outcome="success",
            status_code=200, started=started, account_number=account_number,
            channel=channel, request_body=req_audit, response_body=result,
            correlation_id=corr,
        )
        return result

    if resp.status_code == 403:
        # BUG FIX: Pass the actual error from Core Banking (BLOCKED/EXPIRED/CANCELLED/
        # account not ACTIVE) rather than a hardcoded locked response with zeros.
        try:
            cb_detail = resp.json().get("error", "Card or account is not accessible.")
        except Exception:
            cb_detail = resp.text or "Card or account is not accessible."
        body = {"status": "locked", "message": cb_detail, "remaining_lock_seconds": 0, "lock_minutes": 0}
        correlation.log_step(
            corr, "core_banking_response", "core_banking", "error",
            account_number=account_number, endpoint="/atm/login",
            message=cb_detail,
        )
        _audit(
            endpoint="/atm/login", http_method="POST", outcome="success",
            status_code=200, started=started, account_number=account_number,
            channel=channel, request_body=req_audit, response_body=body,
            correlation_id=corr,
        )
        return body

    if not resp.ok:
        correlation.log_step(
            corr, "core_banking_response", "core_banking", "error",
            account_number=account_number, endpoint="/atm/login",
            message=resp.text, detail={"status_code": resp.status_code},
        )
        raise HTTPException(502, f"Core Banking error: {resp.text}")

    # ── Step 4: successful login ──────────────────────────────────────────────
    data = resp.json()
    account_number = data["accountNumber"]

    correlation.log_step(
        corr, "core_banking_response", "core_banking", "ok",
        account_number=account_number, endpoint="/atm/login",
    )

    # Clear lockout counter keyed by accountNumber.
    lockouts.reset(account_number)

    session_token = sessions.create(
        jwt=data["token"],
        account_id=int(data["accountId"]),
        account_number=account_number,
        card_number=req.cardNumber,
        balance=float(data.get("balance", 0)),
        customer_name=data.get("customerName", "Customer"),
    )

    body = {
        "status":        "ok",
        "sessionToken":  session_token,
        "customerName":  data.get("customerName", "Customer"),
        "cardNumber":    req.cardNumber,
        "accountNumber": account_number,
        "balance":       float(data.get("balance", 0)),
        "account": {
            "account_id": account_number,
            "name":       data.get("customerName", "Customer"),
            "balance":    float(data.get("balance", 0)),
        },
    }
    safe_body = {**body, "sessionToken": "***REDACTED***"}
    correlation.log_step(
        corr, "session_create", "middleware", "ok",
        account_number=account_number, endpoint="/atm/login",
    )
    _audit(
        endpoint="/atm/login", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, request_body=req_audit, response_body=safe_body,
        correlation_id=corr,
    )
    return body


@app.post("/atm/admin/login-unlock")
def atm_admin_login_unlock(
    req: AdminUnlockRequest,
    x_service_token: str | None = Header(None, alias="X-Service-Token"),
):
    """Staff unlock after identity check; customer must set a new PIN at the ATM."""
    _require_service_token(x_service_token)
    lockouts.admin_unlock(req.accountNumber)
    return {
        "status":        "ok",
        "accountNumber": req.accountNumber,
        "mustResetPin":  True,
        "message":       "Account unlocked. Customer must set a new PIN at the ATM.",
    }


@app.post("/atm/reset-pin")
def atm_reset_pin(
    req: ResetPinRequest,
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    """Customer sets new PIN after admin unlock (no old PIN required)."""
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    corr = correlation.new_correlation_id()
    req_audit = {
        "cardNumber": req.cardNumber,
        "newPin":     "***REDACTED***",
        "confirmPin": "***REDACTED***",
    }

    if req.newPin != req.confirmPin:
        body = {"status": "error", "message": "PINs do not match."}
        _audit(
            endpoint="/atm/reset-pin", http_method="POST", outcome="error",
            status_code=200, started=started, account_number=req.cardNumber,
            channel=channel, request_body=req_audit, response_body=body,
            correlation_id=corr,
        )
        return body

    if len(req.newPin) != 4 or not req.newPin.isdigit():
        body = {"status": "error", "message": "PIN must be exactly 4 digits."}
        _audit(
            endpoint="/atm/reset-pin", http_method="POST", outcome="error",
            status_code=200, started=started, account_number=req.cardNumber,
            channel=channel, request_body=req_audit, response_body=body,
            correlation_id=corr,
        )
        return body

    # Resolve card → account number so we check/clear the lockout by accountNumber.
    account_number = _resolve_card_to_account(req.cardNumber)
    if account_number is None:
        raise HTTPException(404, "Card not found.")

    if not lockouts.requires_pin_reset(account_number):
        raise HTTPException(
            403,
            "PIN reset is not required for this account. Contact the bank if you need help.",
        )

    resp = _cb_post_service(
        "/atm/reset-pin",
        {"cardNumber": req.cardNumber, "pin": req.newPin},
    )
    if not resp.ok:
        try:
            err_msg = resp.json().get("error", resp.text)
        except Exception:
            err_msg = resp.text or "Could not reset PIN."
        body = {"status": "error", "message": err_msg}
        _audit(
            endpoint="/atm/reset-pin", http_method="POST", outcome="error",
            status_code=200, started=started, account_number=account_number,
            channel=channel, request_body=req_audit, response_body=body,
            correlation_id=corr,
        )
        return body

    lockouts.complete_pin_reset(account_number)
    body = {
        "status":  "ok",
        "message": "PIN updated. Please log in with your new PIN.",
    }
    _audit(
        endpoint="/atm/reset-pin", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, request_body=req_audit, response_body=body,
        correlation_id=corr,
    )
    return body


@app.post("/atm/create-card")
def atm_create_card(req: CreateCardRequest):
    """
    Step 1 of self-service card setup.
 
    ATM UI sends account number → middleware forwards to Core Banking
    POST /atm/create-card-for-account with X-Service-Token.
 
    Core Banking:
      - Verifies account exists and is ACTIVE
      - Enforces MAX_CARDS_PER_ACCOUNT (3) limit
      - Issues a new card with NO PIN set
      - Returns cardId + masked card number
 
    Middleware returns the same payload to the ATM UI so it can proceed
    to the PIN-entry step.
    """
    raise HTTPException(
        409,
        "ATM setup does not create cards. Ask admin to issue a card first, then set PIN using account number."
    )
 
 
@app.post("/atm/set-own-pin")
def atm_set_own_pin(req: SetOwnPinRequest):
    """
    Step 2 of self-service card setup.
 
    ATM UI collects PIN twice; middleware validates they match and that the
    PIN is exactly 4 digits before forwarding to Core Banking.
 
    Core Banking:
      - Verifies card exists and has no PIN set yet (first-time only)
      - BCrypt-hashes and stores the PIN
      - Returns success; card is now fully active
 
    After this call the customer can log in with their card number + PIN.
    """
    if req.pin != req.pinConfirm:
        raise HTTPException(400, "PINs do not match. Please try again.")
 
    if not req.pin.isdigit() or len(req.pin) != 4:
        raise HTTPException(400, "PIN must be exactly 4 digits.")

    account_number = (req.accountNumber or "").strip().upper()
    if not account_number:
        raise HTTPException(400, "accountNumber is required.")
 
    resp = cb_http.post(
        f"{CORE_BANKING_URL}/atm/set-own-pin",
        json={"cardId": str(req.cardId), "accountNumber": account_number, "pin": req.pin},
        headers={"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"},
        timeout=(3, 12),
    )
    if not resp.ok:
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(resp.status_code, detail)
 
    return resp.json()


@app.post("/atm/prepare-own-pin")
def atm_prepare_own_pin(req: PrepareOwnPinRequest):
    """
    Existing-card activation flow.
    Validates card/account pair and returns cardId so ATM can set PIN on that exact card.
    """
    account_number = (req.accountNumber or "").strip().upper()

    if not account_number:
        raise HTTPException(400, "accountNumber is required.")

    payload: dict = {"accountNumber": account_number}
    if req.cardNumber:
        payload["cardNumber"] = req.cardNumber.strip().replace(" ", "")

    resp = cb_http.post(
        f"{CORE_BANKING_URL}/atm/prepare-own-pin",
        json=payload,
        headers={"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"},
        timeout=(3, 12),
    )
    if not resp.ok:
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(resp.status_code, detail)

    return resp.json()


@app.post("/atm/register-fingerprint")
def atm_register_fingerprint(req: RegisterFingerprintRequest):
    """
    Persist fingerprint sensor slot id on the account after first-time enrollment.
    """
    account_number = (req.accountNumber or "").strip().upper()
    if not account_number:
        raise HTTPException(400, "accountNumber is required.")
    if req.fingerprintSlotId < 0:
        raise HTTPException(400, "fingerprintSlotId must be non-negative.")

    resp = cb_http.post(
        f"{CORE_BANKING_URL}/atm/register-fingerprint",
        json={
            "accountNumber": account_number,
            "fingerprintSlotId": req.fingerprintSlotId,
        },
        headers={"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"},
        timeout=(3, 12),
    )
    if not resp.ok:
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(resp.status_code, detail)

    return resp.json()
 
 
@app.get("/atm/card-setup-status/{account_number}")
def atm_card_setup_status(account_number: str):
    """
    Called by the ATM UI on the card-setup landing page to tell the customer
    how many cards they already have on this account (and whether they can
    create another one), without requiring a login session.
 
    Returns: { hasCards: bool, cardCount: int, canCreateMore: bool }
    """
    # We proxy through Core Banking's resolve-card equivalent.
    # Since /customers/*/accounts requires ROLE_ADMIN or ROLE_USER,
    # we use the service token path to look up the account.
    resp = cb_http.get(
        f"{CORE_BANKING_URL}/atm/resolve-card",
        params={"accountNumber": account_number},   # hypothetical – see note below
        timeout=(3, 8),
    )
    # NOTE: Core Banking's /atm/resolve-card only accepts cardNumber today.
    # If you want to look up by accountNumber without a session, add a
    # GET /atm/account-info?accountNumber=... endpoint to Core Banking
    # (ROLE_SERVICE gated) that returns basic account status + card count.
    # Deprecated status endpoint kept for compatibility only.
    return {"status": "ok", "message": "Proceed to PIN setup."}


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
                      created_at: str,
                      *,
                      correlation_id: str | None = None) -> tuple[str, str | None]:
    """
    Compute the canonical hash, submit to chain, and PATCH the row in Core
    Banking so the worker has durable bookkeeping. On any failure the row
    stays at chainStatus=PENDING_SUBMIT and the worker will retry it later.
    Returns (canonical_hash, blockchain_tx_or_None).
    """
    if correlation_id:
        correlation.log_step(
            correlation_id, "blockchain_hash_compute", "middleware", "ok",
            account_number=account_number,
            detail={"transaction_id": transaction_id, "transaction_type": transaction_type},
        )

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
        if correlation_id:
            correlation.log_step(
                correlation_id, "blockchain_submit", "sepolia",
                "ok" if bc_tx else "error",
                account_number=account_number,
                detail={"transaction_id": transaction_id, "canonical_hash": c_hash},
                message=None if bc_tx else "submit returned no tx hash",
            )
    except Exception as e:
        bc_tx = None
        print(f"[Middleware] inline submit failed for tx {transaction_id}: {e}")
        if correlation_id:
            correlation.log_step(
                correlation_id, "blockchain_submit", "sepolia", "error",
                account_number=account_number, message=str(e),
                detail={"transaction_id": transaction_id},
            )

    admin = _get_admin_client()
    if admin:
        try:
            admin.patch_blockchain(
                transaction_id=transaction_id,
                canonical_hash=c_hash,
                blockchain_tx=bc_tx,
                submit_error=None if bc_tx else "inline submit failed",
            )
            if correlation_id:
                correlation.log_step(
                    correlation_id, "core_banking_blockchain_patch", "core_banking", "ok",
                    account_number=account_number,
                    detail={"transaction_id": transaction_id},
                )
        except Exception as e:
            print(f"[Middleware] PATCH /admin/transactions/{transaction_id}/blockchain failed: {e}")
            if correlation_id:
                correlation.log_step(
                    correlation_id, "core_banking_blockchain_patch", "core_banking", "error",
                    account_number=account_number, message=str(e),
                    detail={"transaction_id": transaction_id},
                )
    elif correlation_id:
        correlation.log_step(
            correlation_id, "core_banking_blockchain_patch", "core_banking", "skipped",
            account_number=account_number,
            message="admin client not configured",
        )
    return c_hash, bc_tx


@app.post("/atm/deposit")
def atm_deposit(
    req: AmountRequest,
    x_session_token: str = Header(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    started = time.perf_counter()
    idempotency_key = _require_idempotency_key(idempotency_key)
    channel = _resolve_channel(x_channel)
    session       = sessions.get(x_session_token)
    account_id    = session["account_id"]
    account_number = session["account_number"]
    old_balance   = session["balance"]
    jwt           = session["jwt"]
    corr = correlation.new_correlation_id()
    correlation.log_step(
        corr, "request_received", "middleware", "ok",
        account_number=account_number, endpoint="/atm/deposit",
        detail=req.model_dump(),
    )

    cached = idempotency.begin(
        idempotency_key, account_number, "/atm/deposit", req.model_dump()
    )
    if cached is not None:
        correlation.log_step(
            corr, "idempotency_cache_hit", "middleware", "cached",
            account_number=account_number, endpoint="/atm/deposit",
        )
        _audit(
            endpoint="/atm/deposit", http_method="POST", outcome="cached",
            status_code=200, started=started, account_number=account_number,
            channel=channel, idempotency_key=idempotency_key,
            request_body=req.model_dump(), response_body=cached,
            correlation_id=corr,
        )
        return cached

    correlation.log_step(
        corr, "idempotency_claimed", "middleware", "ok",
        account_number=account_number, endpoint="/atm/deposit",
    )

    correlation.log_step(
        corr, "core_banking_request", "core_banking", "ok",
        account_number=account_number, endpoint="/atm/deposit",
        detail={"amount": req.amount},
    )
    resp = _cb_post(f"/accounts/{account_id}/deposit", {"amountDeposit": req.amount}, jwt)
    if not resp.ok:
        correlation.log_step(
            corr, "core_banking_response", "core_banking", "error",
            account_number=account_number, endpoint="/atm/deposit",
            message=resp.text, detail={"status_code": resp.status_code},
        )
        raise HTTPException(resp.status_code, resp.text)

    result      = resp.json()
    tx_id       = int(result["transactionId"])
    new_balance = float(result.get("balanceAfter", 0))
    ref_id      = str(result.get("referenceId", ""))
    created_at  = str(result.get("createdAt", datetime.now(timezone.utc).isoformat()))
    sessions.update_balance(x_session_token, new_balance)
    correlation.log_step(
        corr, "core_banking_response", "core_banking", "ok",
        account_number=account_number, endpoint="/atm/deposit",
        detail={"transaction_id": tx_id, "balance_after": new_balance},
    )

    c_hash, bc_tx = _hash_and_persist(
        transaction_id=tx_id,
        account_number=account_number,
        transaction_type="DEPOSIT",
        amount=req.amount,
        balance_after=new_balance,
        reference_id=ref_id,
        created_at=created_at,
        correlation_id=corr,
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

    idempotency.finish(idempotency_key, account_number, response)
    correlation.log_step(
        corr, "idempotency_finish", "middleware", "ok",
        account_number=account_number, endpoint="/atm/deposit",
    )

    _audit(
        endpoint="/atm/deposit", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, idempotency_key=idempotency_key,
        request_body=req.model_dump(), response_body=response,
        correlation_id=corr,
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
    idempotency_key = _require_idempotency_key(idempotency_key)
    channel = _resolve_channel(x_channel)
    session        = sessions.get(x_session_token)
    account_id     = session["account_id"]
    account_number = session["account_number"]
    old_balance    = session["balance"]
    jwt            = session["jwt"]
    corr = correlation.new_correlation_id()
    correlation.log_step(
        corr, "request_received", "middleware", "ok",
        account_number=account_number, endpoint="/atm/withdraw",
        detail=req.model_dump(),
    )

    cached = idempotency.begin(
        idempotency_key, account_number, "/atm/withdraw", req.model_dump()
    )
    if cached is not None:
        correlation.log_step(
            corr, "idempotency_cache_hit", "middleware", "cached",
            account_number=account_number, endpoint="/atm/withdraw",
        )
        _audit(
            endpoint="/atm/withdraw", http_method="POST", outcome="cached",
            status_code=200, started=started, account_number=account_number,
            channel=channel, idempotency_key=idempotency_key,
            request_body=req.model_dump(), response_body=cached,
            correlation_id=corr,
        )
        return cached

    correlation.log_step(
        corr, "idempotency_claimed", "middleware", "ok",
        account_number=account_number, endpoint="/atm/withdraw",
    )

    correlation.log_step(
        corr, "core_banking_request", "core_banking", "ok",
        account_number=account_number, endpoint="/atm/withdraw",
        detail={"amount": req.amount},
    )
    resp = _cb_post(
        f"/accounts/{account_id}/withdraw",
        {"amountWithdraw": req.amount},
        jwt,
        extra_headers={"X-Dispense-Ack-Timeout-Seconds": str(ACK_TIMEOUT_SECONDS)},
    )
    if resp.status_code == 400:
        correlation.log_step(
            corr, "core_banking_response", "core_banking", "error",
            account_number=account_number, endpoint="/atm/withdraw",
            message="insufficient funds",
        )
        raise HTTPException(400, "Insufficient funds")
    if not resp.ok:
        correlation.log_step(
            corr, "core_banking_response", "core_banking", "error",
            account_number=account_number, endpoint="/atm/withdraw",
            message=resp.text, detail={"status_code": resp.status_code},
        )
        raise HTTPException(resp.status_code, resp.text)

    result      = resp.json()
    tx_id       = int(result["transactionId"])
    new_balance = float(result.get("balanceAfter", 0))
    ref_id      = str(result.get("referenceId", ""))
    created_at  = str(result.get("createdAt", datetime.now(timezone.utc).isoformat()))
    sessions.update_balance(x_session_token, new_balance)
    correlation.log_step(
        corr, "core_banking_response", "core_banking", "ok",
        account_number=account_number, endpoint="/atm/withdraw",
        detail={"transaction_id": tx_id, "balance_after": new_balance},
    )

    c_hash, bc_tx = _hash_and_persist(
        transaction_id=tx_id,
        account_number=account_number,
        transaction_type="WITHDRAW",
        amount=req.amount,
        balance_after=new_balance,
        reference_id=ref_id,
        created_at=created_at,
        correlation_id=corr,
    )

    response = {
        "middlewareTxId": tx_id,
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

    idempotency.finish(idempotency_key, account_number, response)
    correlation.log_step(
        corr, "idempotency_finish", "middleware", "ok",
        account_number=account_number, endpoint="/atm/withdraw",
    )

    _audit(
        endpoint="/atm/withdraw", http_method="POST", outcome="success",
        status_code=200, started=started, account_number=account_number,
        channel=channel, idempotency_key=idempotency_key,
        request_body=req.model_dump(), response_body=response,
        correlation_id=corr,
    )
    return response


@app.post("/atm/ack")
def atm_ack(
    req: AckRequest,
    x_session_token: str = Header(...),
    x_channel: str | None = Header(None, alias="X-Channel"),
):
    """ATM calls this after physically dispensing cash — confirms dispense in Core Banking."""
    started = time.perf_counter()
    channel = _resolve_channel(x_channel)
    session = sessions.get(x_session_token)
    account_id = session["account_id"]
    account_number = session["account_number"]
    jwt = session["jwt"]
    tx_id = req.middlewareTxId

    corr = correlation.new_correlation_id()
    correlation.log_step(
        corr, "request_received", "middleware", "ok",
        account_number=account_number, endpoint="/atm/ack",
        detail={"transactionId": tx_id},
    )
    resp = _cb_post(
        f"/accounts/{account_id}/transactions/{tx_id}/confirm-dispense",
        {},
        jwt,
    )
    if not resp.ok:
        correlation.log_step(
            corr, "core_banking_confirm_dispense", "core_banking", "error",
            account_number=account_number, endpoint="/atm/ack",
            message=resp.text, detail={"status_code": resp.status_code},
        )
        raise HTTPException(resp.status_code, resp.text)

    cb_body = resp.json()
    correlation.log_step(
        corr, "core_banking_confirm_dispense", "core_banking", "ok",
        account_number=account_number, endpoint="/atm/ack",
        detail={"dispenseStatus": cb_body.get("dispenseStatus")},
    )

    body = {
        "status":         "CONFIRMED",
        "middlewareTxId": tx_id,
        "transactionId":  tx_id,
        "dispenseStatus": cb_body.get("dispenseStatus", "DISPENSED"),
    }
    _audit(
        endpoint="/atm/ack", http_method="POST", outcome="success",
        status_code=200, started=started,
        account_number=account_number, channel=channel,
        request_body=req.model_dump(), response_body=body,
        correlation_id=corr,
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
        resp = cb_http.get(
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
        resp = cb_http.get(
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
    uvicorn.run("middleware:app", host="127.0.0.1", port=8000, reload=False)