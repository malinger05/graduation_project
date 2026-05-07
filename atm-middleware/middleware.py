"""
middleware.py  —  Layer 2
Runs on port 8000.

Connects the production ATM (Layer 1) to Spring Boot Core Banking (Layer 3).

Key production differences handled:
  - authenticate_with_status() lockout logic preserved
  - create_local_transaction_atomic() replaced with middleware DB write
  - worker.py retry logic mirrored (process_submission_retries_once)
  - Blockchain submission with retry scheduling
  - Atomicity monitor: no ACK within 30s → reverse debit
"""

import hashlib
import inspect
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel


if not hasattr(inspect, "getargspec"):
    inspect.getargspec = inspect.getfullargspec

from web3 import Web3

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

CORE_BANKING_URL = os.environ.get("CORE_BANKING_URL", "http://localhost:8080").rstrip("/")
MIDDLEWARE_DB_URL = os.environ.get("MIDDLEWARE_DB_URL", "postgresql://localhost:5432/middleware")
ACK_TIMEOUT_SECONDS = int(os.environ.get("ACK_TIMEOUT_SECONDS", "30"))

CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "").strip()
ETH_PRIVATE_KEY = os.environ.get("ETH_PRIVATE_KEY", "").strip()
RPC_URL = os.environ.get("ETH_RPC_URL", "https://ethereum-sepolia.publicnode.com").strip()
RPC_FALLBACK_URLS = [
    u.strip() for u in os.environ.get("ETH_RPC_FALLBACK_URLS", "").split(",") if u.strip()
]

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


# ── Lifespan (replaces deprecated @app.on_event) ──────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    get_db()
    threading.Thread(target=_atomicity_monitor, daemon=True).start()
    threading.Thread(target=_blockchain_retry_worker, daemon=True).start()
    print(f"[Middleware] Layer 2 started on port 8000")
    print(f"[Middleware] Core Banking: {CORE_BANKING_URL}")
    print(f"[Middleware] ACK timeout: {ACK_TIMEOUT_SECONDS}s")
    yield
    # Shutdown (nothing needed)


app = FastAPI(title="ATM Middleware — Layer 2", lifespan=lifespan)


# ── Blockchain ────────────────────────────────────────────────────────────────

_blockchain = None
_blockchain_lock = threading.Lock()


def get_blockchain():
    global _blockchain
    with _blockchain_lock:
        if _blockchain is None and CONTRACT_ADDRESS and ETH_PRIVATE_KEY:
            _blockchain = _init_blockchain()
    return _blockchain


def _init_blockchain():
    candidates = [RPC_URL] + RPC_FALLBACK_URLS
    for url in candidates:
        try:
            session = requests.Session()
            session.trust_env = False
            provider = Web3.HTTPProvider(url, request_kwargs={"timeout": 20}, session=session)
            w3 = Web3(provider)
            w3.eth.chain_id
            account = w3.eth.account.from_key(ETH_PRIVATE_KEY)
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(CONTRACT_ADDRESS),
                abi=CONTRACT_ABI,
            )
            print(f"[Blockchain] Connected to {url}")
            return {"w3": w3, "account": account, "contract": contract}
        except Exception as e:
            print(f"[Blockchain] {url} failed: {e}")
    print("[Blockchain] WARNING: No RPC available. Blockchain logging disabled.")
    return None


def submit_to_blockchain(hash_str: str) -> str | None:
    bc = get_blockchain()
    if not bc:
        return None
    try:
        w3, account, contract = bc["w3"], bc["account"], bc["contract"]
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", w3.eth.gas_price)
        priority_fee = w3.to_wei(2, "gwei")
        max_fee = (2 * int(base_fee)) + int(priority_fee)
        tx = contract.functions.storeLog(hash_str).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 200000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "type": 2,
            "chainId": w3.eth.chain_id,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        return tx_hash
    except Exception as e:
        print(f"[Blockchain] submit failed: {e}")
        return None


# ── Middleware Database ───────────────────────────────────────────────────────

_db_conn = None
_db_lock = threading.Lock()


def get_db():
    global _db_conn
    with _db_lock:
        if _db_conn is None or _db_conn.closed:
            _db_conn = psycopg2.connect(MIDDLEWARE_DB_URL)
            _db_conn.autocommit = True
            _init_db(_db_conn)
    return _db_conn


def _init_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS middleware_transactions (
                id              SERIAL PRIMARY KEY,
                account_id      TEXT NOT NULL,
                type            TEXT NOT NULL,
                amount          NUMERIC(14,2) NOT NULL,
                old_balance     NUMERIC(14,2),
                new_balance     NUMERIC(14,2),
                status          TEXT NOT NULL DEFAULT 'PENDING',
                canonical_hash  TEXT,
                blockchain_tx   TEXT,
                core_ref_id     TEXT,
                error_message   TEXT,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ack_received_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_mw_tx_status
                ON middleware_transactions (status, id);
        """)
    print("[DB] middleware_transactions table ready")


def db_create(account_id, tx_type, amount) -> int:
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO middleware_transactions (account_id, type, amount, status)
            VALUES (%s, %s, %s, 'PENDING') RETURNING id
        """, (account_id, tx_type, amount))
        return cur.fetchone()["id"]


def db_success(tx_id, old_bal, new_bal, core_ref, c_hash, bc_tx):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status='SUCCESS', old_balance=%s, new_balance=%s,
                core_ref_id=%s, canonical_hash=%s, blockchain_tx=%s,
                updated_at=NOW()
            WHERE id=%s
        """, (old_bal, new_bal, core_ref, c_hash, bc_tx, tx_id))


def db_failed(tx_id, msg):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status='FAILED', error_message=%s, updated_at=NOW()
            WHERE id=%s
        """, (msg, tx_id))


def db_retry(tx_id, msg):
    """Schedule blockchain retry — mirrors production schedule_submission_retry."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status='RETRY_PENDING', error_message=%s,
                retry_count=retry_count+1, updated_at=NOW()
            WHERE id=%s
        """, (msg, tx_id))


def db_ack(tx_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status='CONFIRMED', ack_received_at=NOW(), updated_at=NOW()
            WHERE id=%s
        """, (tx_id,))


def db_rolled_back(tx_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status='ROLLED_BACK', updated_at=NOW()
            WHERE id=%s
        """, (tx_id,))


def db_update_blockchain(tx_id, bc_tx):
    """Called by retry worker after successful blockchain submit."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                blockchain_tx=%s, status='SUCCESS', updated_at=NOW()
            WHERE id=%s
        """, (bc_tx, tx_id))


def db_get_retry_pending():
    """Get transactions that need blockchain submission retry."""
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id, canonical_hash, retry_count FROM middleware_transactions
            WHERE status='RETRY_PENDING' AND retry_count < 8
            ORDER BY id ASC LIMIT 25
        """)
        return cur.fetchall()


def db_get_success_withdrawals_without_ack():
    """Get withdrawals awaiting cash-dispense ACK."""
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM middleware_transactions
            WHERE status='SUCCESS' AND type='WITHDRAW'
              AND ack_received_at IS NULL
              AND created_at < NOW() - INTERVAL '30 seconds'
        """)
        return cur.fetchall()


# ── Canonical hash ────────────────────────────────────────────────────────────

def make_canonical_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── Core Banking (Spring Boot) client ────────────────────────────────────────

def _cb_post(path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(f"{CORE_BANKING_URL}{path}", json=body,
                         headers=headers, timeout=15)
    return resp


def core_login(account_number: str, pin: str):
    """
    POST /atm/login → Spring Boot AtmAuthController.
    Returns full response dict or None on 401.
    """
    try:
        resp = _cb_post("/atm/login", {"accountNumber": account_number, "pin": pin})
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, f"Cannot reach Core Banking at {CORE_BANKING_URL}")

    if resp.status_code == 401:
        return None, "invalid"
    if resp.status_code == 403:
        return None, "locked"
    if not resp.ok:
        raise HTTPException(502, f"Core Banking error: {resp.text}")
    return resp.json(), "ok"


def core_deposit(account_id: int, amount: float, token: str):
    resp = _cb_post(f"/accounts/{account_id}/deposit",
                    {"amountDeposit": amount}, token)
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()


def core_withdraw(account_id: int, amount: float, token: str):
    resp = _cb_post(f"/accounts/{account_id}/withdraw",
                    {"amountWithdraw": amount}, token)
    if resp.status_code == 400:
        return None  # insufficient funds / inactive
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()


def core_reverse_deposit(account_id: int, amount: float, token: str):
    """Atomicity rollback: re-deposit money from a failed cash dispense."""
    try:
        _cb_post(f"/accounts/{account_id}/deposit",
                 {"amountDeposit": amount}, token)
        print(f"[Rollback] Reversed ${amount} on account {account_id}")
    except Exception as e:
        print(f"[Rollback] ERROR: {e}")


# ── Session store ─────────────────────────────────────────────────────────────
# Maps session_token → {jwt, account_id, account_number, balance, customer_name}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

# Maps middleware_tx_id → {account_id, amount, jwt, deadline}
_pending_acks: dict[int, dict] = {}


def _get_session(token: str) -> dict:
    with _sessions_lock:
        s = _sessions.get(token)
    if not s:
        raise HTTPException(401, "Invalid or expired session. Please log in again.")
    return s


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
    conn = get_db()

    # Check lockout status first
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO atm_login_attempts (account_number)
            VALUES (%s) ON CONFLICT DO NOTHING
        """, (req.accountNumber,))
        cur.execute("""
            SELECT failed_attempts, locked_until
            FROM atm_login_attempts WHERE account_number=%s
        """, (req.accountNumber,))
        row = cur.fetchone()

    if row and row["locked_until"]:
        now = datetime.now(timezone.utc)
        locked_until = row["locked_until"]
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        remaining = int((locked_until - now).total_seconds())
        if remaining > 0:
            return {
                "status": "locked",
                "remaining_lock_seconds": remaining,
                "lock_minutes": remaining // 60,
            }

    # Try login against Spring Boot
    data, status = core_login(req.accountNumber, req.pin)

    if status == "invalid" or data is None:
        # Record failed attempt
        lockout_minutes = [5, 10, 15]
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE atm_login_attempts
                SET failed_attempts = failed_attempts + 1
                WHERE account_number=%s
                RETURNING failed_attempts
            """, (req.accountNumber,))
            updated = cur.fetchone()
            failed = updated["failed_attempts"] if updated else 1

            lock_minutes = 0
            new_locked_until = None
            if failed % 3 == 0:
                level = min(failed // 3, len(lockout_minutes)) - 1
                lock_minutes = lockout_minutes[level]
                new_locked_until = datetime.now(timezone.utc) + timedelta(minutes=lock_minutes)
                cur.execute("""
                    UPDATE atm_login_attempts SET locked_until=%s
                    WHERE account_number=%s
                """, (new_locked_until, req.accountNumber,))

            attempts_to_next = 3 - (failed % 3)

        if lock_minutes:
            return {
                "status": "locked",
                "remaining_lock_seconds": lock_minutes * 60,
                "lock_minutes": lock_minutes,
            }
        return {
            "status": "invalid",
            "attempts_to_next_lock": attempts_to_next,
        }

    # Successful login — reset failed attempts
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE atm_login_attempts
            SET failed_attempts=0, locked_until=NULL
            WHERE account_number=%s
        """, (req.accountNumber,))

    import secrets as _sec
    session_token = _sec.token_hex(32)
    with _sessions_lock:
        _sessions[session_token] = {
            "jwt": data["token"],
            "account_id": int(data["accountId"]),
            "account_number": req.accountNumber,
            "balance": float(data.get("balance", 0)),
            "customer_name": data.get("customerName", "Customer"),
        }

    return {
        "status": "ok",
        "sessionToken": session_token,
        "customerName": data.get("customerName", "Customer"),
        "accountNumber": req.accountNumber,
        "balance": float(data.get("balance", 0)),
        "account": {
            "account_id": req.accountNumber,
            "name": data.get("customerName", "Customer"),
            "balance": float(data.get("balance", 0)),
        }
    }


@app.post("/atm/deposit")
def atm_deposit(req: AmountRequest, x_session_token: str = Header(...)):
    session = _get_session(x_session_token)
    account_number = session["account_number"]
    account_id = session["account_id"]
    jwt = session["jwt"]
    old_balance = session["balance"]

    # 1. Record PENDING
    tx_id = db_create(account_number, "DEPOSIT", req.amount)

    try:
        # 2. Call Spring Boot
        result = core_deposit(account_id, req.amount, jwt)
        new_balance = old_balance + req.amount
        session["balance"] = new_balance

        # 3. Canonical hash
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "account_id": account_number,
            "type": "DEPOSIT",
            "amount": float(req.amount),
            "old_balance": old_balance,
            "new_balance": new_balance,
            "created_at": created_at,
        }
        c_hash = make_canonical_hash(payload)

        # 4. Blockchain (with retry fallback like production worker.py)
        bc_tx = None
        try:
            bc_tx = submit_to_blockchain(c_hash)
        except Exception as exc:
            db_retry(tx_id, str(exc))
            # Still return success — worker will retry blockchain
            db_success(tx_id, old_balance, new_balance,
                       str(result.get("referenceId", "")), c_hash, None)
            return {
                "middlewareTxId": tx_id,
                "status": "SUCCESS",
                "amount": req.amount,
                "oldBalance": old_balance,
                "newBalance": new_balance,
                "canonicalHash": c_hash,
                "blockchainTx": None,
                "verifyUrl": None,
                "message": "Recorded locally; blockchain sync will retry shortly.",
            }

        # 5. Record SUCCESS
        db_success(tx_id, old_balance, new_balance,
                   str(result.get("referenceId", "")), c_hash, bc_tx)

        return {
            "middlewareTxId": tx_id,
            "status": "SUCCESS",
            "amount": req.amount,
            "oldBalance": old_balance,
            "newBalance": new_balance,
            "canonicalHash": c_hash,
            "blockchainTx": bc_tx,
            "verifyUrl": f"https://sepolia.etherscan.io/tx/{bc_tx}" if bc_tx else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        db_failed(tx_id, str(e))
        raise HTTPException(500, f"Deposit failed: {e}")


@app.post("/atm/withdraw")
def atm_withdraw(req: AmountRequest, x_session_token: str = Header(...)):
    session = _get_session(x_session_token)
    account_number = session["account_number"]
    account_id = session["account_id"]
    jwt = session["jwt"]
    old_balance = session["balance"]

    # 1. Record PENDING
    tx_id = db_create(account_number, "WITHDRAW", req.amount)

    try:
        # 2. Call Spring Boot
        result = core_withdraw(account_id, req.amount, jwt)
        if result is None:
            db_failed(tx_id, "Insufficient funds")
            raise HTTPException(400, "Insufficient funds")

        new_balance = old_balance - req.amount
        session["balance"] = new_balance

        # 3. Canonical hash
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "account_id": account_number,
            "type": "WITHDRAW",
            "amount": float(req.amount),
            "old_balance": old_balance,
            "new_balance": new_balance,
            "created_at": created_at,
        }
        c_hash = make_canonical_hash(payload)

        # 4. Blockchain with retry fallback
        bc_tx = None
        try:
            bc_tx = submit_to_blockchain(c_hash)
        except Exception as exc:
            db_retry(tx_id, str(exc))

        # 5. Record SUCCESS — awaiting ACK
        db_success(tx_id, old_balance, new_balance,
                   str(result.get("referenceId", "")), c_hash, bc_tx)

        # 6. Register atomicity timer
        _pending_acks[tx_id] = {
            "account_id": account_id,
            "amount": req.amount,
            "jwt": jwt,
            "deadline": time.time() + ACK_TIMEOUT_SECONDS,
        }

        return {
            "middlewareTxId": tx_id,
            "status": "SUCCESS",
            "amount": req.amount,
            "oldBalance": old_balance,
            "newBalance": new_balance,
            "canonicalHash": c_hash,
            "blockchainTx": bc_tx,
            "verifyUrl": f"https://sepolia.etherscan.io/tx/{bc_tx}" if bc_tx else None,
            "message": "Dispense cash now, then call /atm/ack",
        }

    except HTTPException:
        raise
    except Exception as e:
        db_failed(tx_id, str(e))
        raise HTTPException(500, f"Withdraw failed: {e}")


@app.post("/atm/ack")
def atm_ack(req: AckRequest, x_session_token: str = Header(...)):
    """ATM calls this after physically dispensing cash."""
    _get_session(x_session_token)
    _pending_acks.pop(req.middlewareTxId, None)
    db_ack(req.middlewareTxId)
    return {"status": "CONFIRMED", "middlewareTxId": req.middlewareTxId}


@app.get("/atm/balance")
def get_balance(x_session_token: str = Header(...)):
    session = _get_session(x_session_token)
    return {"balance": session["balance"],
            "accountNumber": session["account_number"]}


@app.get("/atm/transactions")
def get_transactions(x_session_token: str = Header(...)):
    session = _get_session(x_session_token)
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id, type, amount, old_balance, new_balance,
                   status, canonical_hash, blockchain_tx, created_at
            FROM middleware_transactions
            WHERE account_id=%s
            ORDER BY created_at DESC LIMIT 20
        """, (session["account_number"],))
        return [dict(r) for r in cur.fetchall()]


# ── Background: atomicity monitor ────────────────────────────────────────────

def _atomicity_monitor():
    """
    Mirrors UML sequence diagram failure path.
    No ACK within 30s → reverse debit on Spring Boot → ROLLED_BACK.
    """
    while True:
        now = time.time()
        expired = [(tid, d) for tid, d in list(_pending_acks.items())
                   if now > d["deadline"]]
        for tx_id, data in expired:
            _pending_acks.pop(tx_id, None)
            print(f"[Atomicity] No ACK for tx #{tx_id} — rolling back ${data['amount']}")
            core_reverse_deposit(data["account_id"], data["amount"], data["jwt"])
            db_rolled_back(tx_id)
        time.sleep(1)


# ── Background: blockchain retry worker ──────────────────────────────────────

def _blockchain_retry_worker():
    """
    Mirrors production worker.py / process_submission_retries_once().
    Retries blockchain submission for RETRY_PENDING transactions.
    """
    while True:
        try:
            for txn in db_get_retry_pending():
                try:
                    bc_tx = submit_to_blockchain(txn["canonical_hash"])
                    if bc_tx:
                        db_update_blockchain(txn["id"], bc_tx)
                        print(f"[RetryWorker] tx #{txn['id']} submitted: {bc_tx}")
                    else:
                        db_retry(txn["id"], "Blockchain unavailable on retry")
                except Exception as exc:
                    db_retry(txn["id"], str(exc))
        except Exception:
            pass
        time.sleep(10)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("middleware:app", host="127.0.0.1", port=8000, reload=True)