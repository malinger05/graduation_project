"""
middleware.py  —  Layer 2
Runs on port 8000.

Pure bridge: ATM → Middleware → Core Banking (Spring Boot).

Responsibilities:
  - Forward login / deposit / withdraw to Core Banking
  - Track login lockouts in memory
  - Manage sessions in memory (with TTL expiry)
  - Hash the confirmed transaction data and log it to Ethereum Sepolia
  - ACK / atomicity timer for withdrawals (in memory)

Intentionally NOT here:
  - No database. No PostgreSQL. Core Banking owns all data.
  - No balance calculation. Core Banking returns the new balance.
  - No transaction records. Core Banking stores transactions.
"""

import hashlib
import inspect
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

if not hasattr(inspect, "getargspec"):
    inspect.getargspec = inspect.getfullargspec

from web3 import Web3

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

CORE_BANKING_URL      = os.environ.get("CORE_BANKING_URL", "http://localhost:8080").rstrip("/")
ACK_TIMEOUT_SECONDS   = int(os.environ.get("ACK_TIMEOUT_SECONDS", "30"))
SESSION_TTL_SECONDS   = int(os.environ.get("SESSION_TTL_SECONDS", "1800"))  # 30 min
LOCKOUT_MAX_ATTEMPTS  = int(os.environ.get("LOCKOUT_MAX_ATTEMPTS", "3"))
LOCKOUT_MINUTES       = [5, 10, 15]  # progressive lockout durations

CONTRACT_ADDRESS  = os.environ.get("CONTRACT_ADDRESS", "").strip()
ETH_PRIVATE_KEY   = os.environ.get("ETH_PRIVATE_KEY", "").strip()
RPC_URL           = os.environ.get("ETH_RPC_URL", "https://ethereum-sepolia.publicnode.com").strip()
RPC_FALLBACK_URLS = [u.strip() for u in os.environ.get("ETH_RPC_FALLBACK_URLS", "").split(",") if u.strip()]

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


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_atomicity_monitor, daemon=True).start()
    threading.Thread(target=_session_cleanup, daemon=True).start()
    print(f"[Middleware] Layer 2 started on port 8000")
    print(f"[Middleware] Core Banking: {CORE_BANKING_URL}")
    print(f"[Middleware] No database — Core Banking owns all data.")
    yield


app = FastAPI(title="ATM Middleware — Layer 2", lifespan=lifespan)


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


def _make_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── In-memory session store ───────────────────────────────────────────────────
# Maps session_token → {jwt, account_id, account_number, balance,
#                        customer_name, last_active}

_sessions:      dict[str, dict] = {}
_sessions_lock: threading.Lock  = threading.Lock()


def _get_session(token: str) -> dict:
    with _sessions_lock:
        s = _sessions.get(token)
        if not s:
            raise HTTPException(401, "Invalid or expired session. Please log in again.")
        s["last_active"] = time.time()
        return s


def _remove_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


def _session_cleanup() -> None:
    """Evict sessions idle longer than SESSION_TTL_SECONDS. Runs every 60s."""
    while True:
        time.sleep(60)
        cutoff = time.time() - SESSION_TTL_SECONDS
        with _sessions_lock:
            stale = [k for k, v in _sessions.items() if v.get("last_active", 0) < cutoff]
            for k in stale:
                del _sessions[k]
        if stale:
            print(f"[Sessions] Evicted {len(stale)} idle session(s)")


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
def atm_login(req: LoginRequest):
    # 1. Check lockout (in memory — no DB)
    lockout = _check_lockout(req.accountNumber)
    if lockout:
        return {"status": "locked", **lockout}

    # 2. Forward to Core Banking
    resp = _cb_post("/atm/login", {"accountNumber": req.accountNumber, "pin": req.pin})

    if resp.status_code == 401:
        result = _record_failed_attempt(req.accountNumber)
        return result

    if resp.status_code == 403:
        return {"status": "locked", "remaining_lock_seconds": 0, "lock_minutes": 0}

    if not resp.ok:
        raise HTTPException(502, f"Core Banking error: {resp.text}")

    # 3. Successful — reset lockout, create session
    data = resp.json()
    _reset_lockout(req.accountNumber)

    session_token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[session_token] = {
            "jwt":            data["token"],
            "account_id":     int(data["accountId"]),
            "account_number": req.accountNumber,
            "balance":        float(data.get("balance", 0)),
            "customer_name":  data.get("customerName", "Customer"),
            "last_active":    time.time(),
        }

    return {
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


@app.post("/atm/logout")
def atm_logout(x_session_token: str = Header(...)):
    _remove_session(x_session_token)
    return {"status": "logged_out"}


@app.post("/atm/deposit")
def atm_deposit(req: AmountRequest, x_session_token: str = Header(...)):
    session       = _get_session(x_session_token)
    account_id    = session["account_id"]
    account_number = session["account_number"]
    old_balance   = session["balance"]
    jwt           = session["jwt"]

    # 1. Forward to Core Banking — it validates, updates balance, saves transaction
    resp = _cb_post(f"/accounts/{account_id}/deposit", {"amountDeposit": req.amount}, jwt)
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)

    # 2. Read authoritative result — Core Banking did all the math
    result      = resp.json()
    new_balance = float(result.get("balanceAfter", 0))
    ref_id      = str(result.get("referenceId", ""))
    created_at  = str(result.get("createdAt", datetime.now(timezone.utc).isoformat()))
    session["balance"] = new_balance

    # 3. Hash confirmed data and log to blockchain — middleware is just the witness
    payload = {
        "account_id":  account_number,
        "type":        "DEPOSIT",
        "amount":      float(req.amount),
        "old_balance": old_balance,
        "new_balance": new_balance,
        "ref_id":      ref_id,
        "created_at":  created_at,
    }
    c_hash = _make_hash(payload)
    bc_tx  = _submit_to_blockchain(c_hash)

    return {
        "status":        "SUCCESS",
        "amount":        req.amount,
        "oldBalance":    old_balance,
        "newBalance":    new_balance,
        "canonicalHash": c_hash,
        "blockchainTx":  bc_tx,
        "verifyUrl":     f"https://sepolia.etherscan.io/tx/{bc_tx}" if bc_tx else None,
        "referenceId":   ref_id,
        "message":       "" if bc_tx else "Blockchain sync unavailable — transaction still recorded in Core Banking.",
    }


@app.post("/atm/withdraw")
def atm_withdraw(req: AmountRequest, x_session_token: str = Header(...)):
    session        = _get_session(x_session_token)
    account_id     = session["account_id"]
    account_number = session["account_number"]
    old_balance    = session["balance"]
    jwt            = session["jwt"]

    # 1. Forward to Core Banking — it validates funds, subtracts balance, saves transaction
    resp = _cb_post(f"/accounts/{account_id}/withdraw", {"amountWithdraw": req.amount}, jwt)
    if resp.status_code == 400:
        raise HTTPException(400, "Insufficient funds")
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)

    # 2. Read authoritative result — Core Banking did all the math
    result      = resp.json()
    new_balance = float(result.get("balanceAfter", 0))
    ref_id      = str(result.get("referenceId", ""))
    created_at  = str(result.get("createdAt", datetime.now(timezone.utc).isoformat()))
    session["balance"] = new_balance

    # 3. Hash confirmed data and log to blockchain
    payload = {
        "account_id":  account_number,
        "type":        "WITHDRAW",
        "amount":      float(req.amount),
        "old_balance": old_balance,
        "new_balance": new_balance,
        "ref_id":      ref_id,
        "created_at":  created_at,
    }
    c_hash = _make_hash(payload)
    bc_tx  = _submit_to_blockchain(c_hash)

    # 4. Register ACK timer — if ATM doesn't confirm cash dispensed within
    #    ACK_TIMEOUT_SECONDS, atomicity monitor re-deposits the amount
    tx_id = _new_ack_id()
    _pending_acks[tx_id] = {
        "account_id": account_id,
        "amount":     req.amount,
        "jwt":        jwt,
        "deadline":   time.time() + ACK_TIMEOUT_SECONDS,
    }

    return {
        "middlewareTxId": tx_id,
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


@app.post("/atm/ack")
def atm_ack(req: AckRequest, x_session_token: str = Header(...)):
    """ATM calls this after physically dispensing cash — cancels the rollback timer."""
    _get_session(x_session_token)
    _pending_acks.pop(req.middlewareTxId, None)
    return {"status": "CONFIRMED", "middlewareTxId": req.middlewareTxId}


@app.get("/atm/balance")
def get_balance(x_session_token: str = Header(...)):
    """Returns the cached balance. For a live balance, call Core Banking directly."""
    session = _get_session(x_session_token)
    return {"balance": session["balance"], "accountNumber": session["account_number"]}


@app.get("/atm/transactions")
def get_transactions(x_session_token: str = Header(...)):
    """Proxy to Core Banking transaction history — middleware has no DB of its own."""
    session    = _get_session(x_session_token)
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
    return resp.json()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("middleware:app", host="0.0.0.0", port=8000, reload=False)