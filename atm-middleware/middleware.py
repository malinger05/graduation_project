"""
middleware.py  —  Layer 2
Runs on port 8000.

Responsibilities:
  - Receive requests from ATM (Layer 1)
  - Authenticate ATM users via Spring Boot
  - Record every transaction as PENDING in middleware DB
  - Call Spring Boot (Layer 3) to execute the banking operation
  - Update transaction status to SUCCESS or FAILED
  - Write canonical hash to Sepolia blockchain
  - Run atomicity monitor: if no ACK within timeout → reverse debit

ATM   →   middleware:8000   →   Spring Boot:8080
"""

import hashlib
import inspect
import json
import os
import threading
import time
from datetime import datetime, timezone

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

# web3 compat fix
if not hasattr(inspect, "getargspec"):
    inspect.getargspec = inspect.getfullargspec

from web3 import Web3

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

CORE_BANKING_URL = os.environ.get("CORE_BANKING_URL", "http://localhost:8080").rstrip("/")
MIDDLEWARE_DB_URL = os.environ.get("MIDDLEWARE_DB_URL", "postgresql://localhost:5432/middleware")
ACK_TIMEOUT_SECONDS = int(os.environ.get("ACK_TIMEOUT_SECONDS", "30"))

CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "").strip()
ETH_PRIVATE_KEY  = os.environ.get("ETH_PRIVATE_KEY", "").strip()
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

app = FastAPI(title="ATM Middleware — Layer 2")

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
            w3.eth.chain_id  # connectivity test
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
                id SERIAL PRIMARY KEY,
                account_id    TEXT NOT NULL,
                type          TEXT NOT NULL,
                amount        NUMERIC(14,2) NOT NULL,
                old_balance   NUMERIC(14,2),
                new_balance   NUMERIC(14,2),
                status        TEXT NOT NULL DEFAULT 'PENDING',
                canonical_hash TEXT,
                blockchain_tx  TEXT,
                core_banking_tx_id TEXT,
                error_message TEXT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ack_received_at TIMESTAMPTZ
            );
        """)
    print("[DB] middleware_transactions table ready")

def db_create_transaction(account_id, tx_type, amount) -> int:
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO middleware_transactions (account_id, type, amount, status)
            VALUES (%s, %s, %s, 'PENDING') RETURNING id
        """, (account_id, tx_type, amount))
        return cur.fetchone()["id"]

def db_update_success(tx_id, old_balance, new_balance, core_tx_id, canonical_hash, blockchain_tx):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status = 'SUCCESS',
                old_balance = %s,
                new_balance = %s,
                core_banking_tx_id = %s,
                canonical_hash = %s,
                blockchain_tx = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (old_balance, new_balance, core_tx_id, canonical_hash, blockchain_tx, tx_id))

def db_update_failed(tx_id, error_msg):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status = 'FAILED', error_message = %s, updated_at = NOW()
            WHERE id = %s
        """, (error_msg, tx_id))

def db_mark_ack(tx_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status = 'CONFIRMED', ack_received_at = NOW(), updated_at = NOW()
            WHERE id = %s
        """, (tx_id,))

def db_mark_rolled_back(tx_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE middleware_transactions SET
                status = 'ROLLED_BACK', updated_at = NOW()
            WHERE id = %s
        """, (tx_id,))

def db_get_pending_ack():
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM middleware_transactions
            WHERE status = 'SUCCESS' AND type = 'WITHDRAW'
            AND ack_received_at IS NULL
            AND created_at < NOW() - INTERVAL '30 seconds'
        """)
        return cur.fetchall()

# ── Canonical hash ────────────────────────────────────────────────────────────

def make_canonical_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

# ── Core Banking client ───────────────────────────────────────────────────────

def core_login(account_number: str, pin: str) -> dict:
    """POST /atm/login on Spring Boot → returns token + accountId + balance"""
    resp = requests.post(
        f"{CORE_BANKING_URL}/atm/login",
        json={"accountNumber": account_number, "pin": pin},
        timeout=10,
    )
    if resp.status_code == 401:
        return None
    if not resp.ok:
        raise HTTPException(502, f"Core Banking login error: {resp.text}")
    return resp.json()

def core_deposit(account_id: int, amount: float, token: str) -> dict:
    """POST /accounts/{id}/deposit on Spring Boot"""
    resp = requests.post(
        f"{CORE_BANKING_URL}/accounts/{account_id}/deposit",
        json={"amountDeposit": amount},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()

def core_withdraw(account_id: int, amount: float, token: str) -> dict:
    """POST /accounts/{id}/withdraw on Spring Boot"""
    resp = requests.post(
        f"{CORE_BANKING_URL}/accounts/{account_id}/withdraw",
        json={"amountWithdraw": amount},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code == 400:
        return None  # insufficient funds
    if not resp.ok:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()

def core_reverse_deposit(account_id: int, amount: float, token: str):
    """Rollback: re-deposit money that was withdrawn but cash never dispensed"""
    try:
        requests.post(
            f"{CORE_BANKING_URL}/accounts/{account_id}/deposit",
            json={"amountDeposit": amount},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        print(f"[Rollback] Reversed ${amount} for account {account_id}")
    except Exception as e:
        print(f"[Rollback] ERROR reversing ${amount} for account {account_id}: {e}")

# ── Session store (in-memory for local dev) ───────────────────────────────────
# Maps session_token → {jwt, account_id, account_number, balance}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    accountNumber: str
    pin: str

class AmountRequest(BaseModel):
    amount: float

class AckRequest(BaseModel):
    middlewareTxId: int

# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "layer": 2, "service": "ATM Middleware"}


@app.post("/atm/login")
def atm_login(req: LoginRequest):
    """
    Layer 1 (ATM) calls this.
    Forwards to Spring Boot /atm/login.
    Returns session token + balance for ATM to display.
    """
    data = core_login(req.accountNumber, req.pin)
    if not data:
        raise HTTPException(401, "Invalid account number or PIN")

    # Create middleware session
    import secrets
    session_token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[session_token] = {
            "jwt": data["token"],
            "account_id": int(data["accountId"]),
            "account_number": req.accountNumber,
            "balance": float(data.get("balance", 0)),
            "customer_name": data.get("customerName", "Customer"),
        }

    return {
        "sessionToken": session_token,
        "customerName": data.get("customerName", "Customer"),
        "accountNumber": req.accountNumber,
        "balance": float(data.get("balance", 0)),
    }


@app.post("/atm/deposit")
def atm_deposit(req: AmountRequest, x_session_token: str = Header(...)):
    """
    Layer 1 sends deposit request here.
    1. Record PENDING in middleware DB
    2. Call Spring Boot deposit
    3. Record SUCCESS/FAILED
    4. Write blockchain log
    5. Return result to ATM
    """
    session = _get_session(x_session_token)
    account_number = session["account_number"]
    account_id = session["account_id"]
    jwt = session["jwt"]
    old_balance = session["balance"]

    # Step 1: record PENDING
    tx_id = db_create_transaction(account_number, "DEPOSIT", req.amount)

    try:
        # Step 2: call Spring Boot
        result = core_deposit(account_id, req.amount, jwt)

        new_balance = old_balance + req.amount
        session["balance"] = new_balance

        # Step 3: build canonical hash
        payload = {
            "account_id": account_number,
            "type": "DEPOSIT",
            "amount": float(req.amount),
            "old_balance": old_balance,
            "new_balance": new_balance,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        c_hash = make_canonical_hash(payload)

        # Step 4: blockchain log
        blockchain_tx = submit_to_blockchain(c_hash)

        # Step 5: record SUCCESS
        db_update_success(
            tx_id, old_balance, new_balance,
            str(result.get("referenceId", "")),
            c_hash, blockchain_tx
        )

        return {
            "middlewareTxId": tx_id,
            "status": "SUCCESS",
            "amount": req.amount,
            "oldBalance": old_balance,
            "newBalance": new_balance,
            "canonicalHash": c_hash,
            "blockchainTx": blockchain_tx,
            "verifyUrl": f"https://sepolia.etherscan.io/tx/{blockchain_tx}" if blockchain_tx else None,
        }

    except Exception as e:
        db_update_failed(tx_id, str(e))
        raise HTTPException(500, f"Deposit failed: {e}")


@app.post("/atm/withdraw")
def atm_withdraw(req: AmountRequest, x_session_token: str = Header(...)):
    """
    Layer 1 sends withdraw request here.
    1. Record PENDING in middleware DB
    2. Call Spring Boot withdraw
    3. Record SUCCESS — but wait for ACK before CONFIRMED
    4. Atomicity monitor will rollback if no ACK within timeout
    """
    session = _get_session(x_session_token)
    account_number = session["account_number"]
    account_id = session["account_id"]
    jwt = session["jwt"]
    old_balance = session["balance"]

    # Step 1: record PENDING
    tx_id = db_create_transaction(account_number, "WITHDRAW", req.amount)

    try:
        # Step 2: call Spring Boot
        result = core_withdraw(account_id, req.amount, jwt)
        if result is None:
            db_update_failed(tx_id, "Insufficient funds")
            raise HTTPException(400, "Insufficient funds")

        new_balance = old_balance - req.amount
        session["balance"] = new_balance

        # Step 3: build canonical hash
        payload = {
            "account_id": account_number,
            "type": "WITHDRAW",
            "amount": float(req.amount),
            "old_balance": old_balance,
            "new_balance": new_balance,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        c_hash = make_canonical_hash(payload)

        # Step 4: blockchain log
        blockchain_tx = submit_to_blockchain(c_hash)

        # Step 5: record SUCCESS — awaiting ACK
        db_update_success(
            tx_id, old_balance, new_balance,
            str(result.get("referenceId", "")),
            c_hash, blockchain_tx
        )

        # Store JWT for potential rollback
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
            "blockchainTx": blockchain_tx,
            "verifyUrl": f"https://sepolia.etherscan.io/tx/{blockchain_tx}" if blockchain_tx else None,
            "message": "Dispense cash now, then call /atm/ack",
        }

    except HTTPException:
        raise
    except Exception as e:
        db_update_failed(tx_id, str(e))
        raise HTTPException(500, f"Withdraw failed: {e}")


@app.post("/atm/ack")
def atm_ack(req: AckRequest, x_session_token: str = Header(...)):
    """
    ATM calls this AFTER physically dispensing cash.
    Marks transaction as CONFIRMED. Clears rollback timer.
    """
    _get_session(x_session_token)
    _pending_acks.pop(req.middlewareTxId, None)
    db_mark_ack(req.middlewareTxId)
    return {"status": "CONFIRMED", "middlewareTxId": req.middlewareTxId}


@app.get("/atm/transactions")
def get_transactions(x_session_token: str = Header(...)):
    """Returns transaction history from middleware DB for this account."""
    session = _get_session(x_session_token)
    account_number = session["account_number"]
    conn = get_db()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id, type, amount, old_balance, new_balance,
                   status, canonical_hash, blockchain_tx, created_at
            FROM middleware_transactions
            WHERE account_id = %s
            ORDER BY created_at DESC LIMIT 20
        """, (account_number,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/atm/balance")
def get_balance(x_session_token: str = Header(...)):
    session = _get_session(x_session_token)
    return {"balance": session["balance"], "accountNumber": session["account_number"]}


# ── Session helper ────────────────────────────────────────────────────────────

def _get_session(token: str) -> dict:
    with _sessions_lock:
        session = _sessions.get(token)
    if not session:
        raise HTTPException(401, "Invalid or expired session. Please log in again.")
    return session


# ── Atomicity monitor ─────────────────────────────────────────────────────────

_pending_acks: dict[int, dict] = {}

def _atomicity_monitor():
    """
    Background thread.
    If a withdrawal has no ACK within timeout → reverse debit on Spring Boot.
    This is the atomicity guarantee from the UML sequence diagram.
    """
    while True:
        now = time.time()
        expired = [(tx_id, data) for tx_id, data in list(_pending_acks.items())
                   if now > data["deadline"]]
        for tx_id, data in expired:
            _pending_acks.pop(tx_id, None)
            print(f"[Atomicity] No ACK for tx #{tx_id} — rolling back ${data['amount']}")
            core_reverse_deposit(data["account_id"], data["amount"], data["jwt"])
            db_mark_rolled_back(tx_id)
            print(f"[Atomicity] Rollback complete for tx #{tx_id}")
        time.sleep(1)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    get_db()  # init DB
    threading.Thread(target=_atomicity_monitor, daemon=True).start()
    print("[Middleware] Layer 2 started on port 8000")
    print(f"[Middleware] Core Banking: {CORE_BANKING_URL}")
    print(f"[Middleware] ACK timeout: {ACK_TIMEOUT_SECONDS}s")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("middleware:app", host="127.0.0.1", port=8000, reload=True)