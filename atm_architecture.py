"""
atm_architecture.py  —  Layer 1
ATM is a pure UI client. Zero banking logic here.

WHERE EACH RESPONSIBILITY LIVES:
  Layer 1 (this file)   → show UI, send requests, display results
  Layer 2 (middleware)  → atomicity, blockchain logging, session/lockout state
  Layer 3 (Spring Boot) → deposit, withdraw, balance, customer, account data,
                          and the PostgreSQL database (in core-banking-system)
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

MIDDLEWARE_URL = os.environ.get("MIDDLEWARE_URL", "http://localhost:8000").rstrip("/")


# ── Middleware HTTP client ─────────────────────────────────────────────────────

class MiddlewareClient:
    """
    Single HTTP client for all ATM operations.
    Holds the session token returned at login.
    Never touches PostgreSQL or blockchain directly — middleware owns both.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url
        self._session_token: str | None = None
        self._customer_name: str = "Customer"
        self._account_number: str | None = None
        self._cached_balance: float = 0.0

    def _headers(self) -> dict:
        if not self._session_token:
            raise RuntimeError("Not logged in.")
        return {
            "x-session-token": self._session_token,
            "X-Channel":       "ATM_WEB",
        }

    def _mutation_headers(self, idempotency_key: str) -> dict:
        """Session token + Idempotency-Key (required for deposit/withdraw per spec)."""
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required for financial operations")
        headers = self._headers()
        headers["Idempotency-Key"] = idempotency_key.strip()
        return headers

    # ── Auth ──────────────────────────────────────────────────────────────────

    def authenticate_with_status(self, account_number: str, pin: str) -> dict:
        """
        POST /atm/login → middleware → Spring Boot /atm/login.
        Spring Boot verifies BCrypt PIN and returns a JWT.
        Middleware creates a session token and returns it to ATM.
        """
        try:
            resp = requests.post(
                f"{self.base_url}/atm/login",
                json={"accountNumber": account_number, "pin": pin},
                headers={"X-Channel": "ATM_WEB"},
                timeout=10,
            )
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Cannot reach middleware at {self.base_url}.\n"
                "Start it: cd atm-middleware && python3 middleware.py"
            )

        data = resp.json()
        status = data.get("status", "invalid")

        if status == "locked":
            return {
                "status": "locked",
                "remaining_lock_seconds": data.get("remaining_lock_seconds", 300),
                "lock_minutes": data.get("lock_minutes"),
            }

        if status != "ok":
            return {
                "status": "invalid",
                "attempts_to_next_lock": data.get("attempts_to_next_lock"),
            }

        self._session_token = data["sessionToken"]
        self._customer_name = data.get("customerName", "Customer")
        self._account_number = account_number
        self._cached_balance = float(data.get("balance", 0))

        return {
            "status": "ok",
            "account": {
                "account_id": account_number,
                "name": self._customer_name,
                "balance": self._cached_balance,
            },
        }

    def authenticate(self, account_number: str, pin: str):
        result = self.authenticate_with_status(account_number, pin)
        if result.get("status") != "ok":
            return None
        return result.get("account")

    # ── Balance ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Cached from login, updated after each transaction by middleware response."""
        return self._cached_balance

    def get_account(self) -> dict | None:
        if not self._session_token:
            return None
        return {
            "account_id": self._account_number,
            "name": self._customer_name,
            "balance": self._cached_balance,
        }

    # ── Deposit ───────────────────────────────────────────────────────────────

    def deposit(self, amount: float, *, idempotency_key: str) -> tuple[bool, str, dict | None]:
        """
        POST /atm/deposit → middleware → Spring Boot /accounts/{id}/deposit.
        Spring Boot acquires a pessimistic lock, updates the balance, persists
        the transaction. Middleware logs the canonical hash to Sepolia.
        """
        try:
            resp = requests.post(
                f"{self.base_url}/atm/deposit",
                json={"amount": amount},
                headers=self._mutation_headers(idempotency_key),
                timeout=30,
            )
        except requests.exceptions.ConnectionError:
            return False, "Cannot reach middleware", None

        if not resp.ok:
            return False, f"Deposit failed: {resp.text}", None

        data = resp.json()
        self._cached_balance = float(data.get("newBalance", self._cached_balance))

        msg = f"DEPOSIT ${amount:.2f}. New balance: ${self._cached_balance:.2f}"
        if not data.get("blockchainTx"):
            msg += " Recorded locally; blockchain sync will retry shortly."

        return True, msg, data

    # ── Withdraw ──────────────────────────────────────────────────────────────

    def withdraw(self, amount: float, *, idempotency_key: str) -> tuple[bool, str, dict | None]:
        """
        POST /atm/withdraw → middleware → Spring Boot /accounts/{id}/withdraw.
        Middleware starts a 30s ACK timer; if ATM doesn't confirm cash dispensed,
        the debit is reversed (atomicity rollback).
        """
        try:
            resp = requests.post(
                f"{self.base_url}/atm/withdraw",
                json={"amount": amount},
                headers=self._mutation_headers(idempotency_key),
                timeout=30,
            )
        except requests.exceptions.ConnectionError:
            return False, "Cannot reach middleware", None

        if resp.status_code == 400:
            return False, "Insufficient funds", None
        if not resp.ok:
            return False, f"Withdraw failed: {resp.text}", None

        data = resp.json()
        self._cached_balance = float(data.get("newBalance", self._cached_balance))

        # Send ACK — simulates physical cash-dispense confirmation.
        # On real ATM hardware this fires AFTER the cash drawer opens.
        mid_tx_id = data.get("middlewareTxId")
        if mid_tx_id:
            try:
                requests.post(
                    f"{self.base_url}/atm/ack",
                    json={"middlewareTxId": mid_tx_id},
                    headers=self._headers(),
                    timeout=10,
                )
            except Exception:
                # No ACK → atomicity monitor will automatically reverse the debit.
                pass

        msg = f"WITHDRAW ${amount:.2f}. New balance: ${self._cached_balance:.2f}"
        if not data.get("blockchainTx"):
            msg += " Recorded locally; blockchain sync will retry shortly."

        return True, msg, data

    # ── Transaction history ───────────────────────────────────────────────────

    def get_transactions_for_account(self, account_id, limit=10) -> list:
        """GET /atm/transactions → middleware proxies to Core Banking."""
        try:
            resp = requests.get(
                f"{self.base_url}/atm/transactions",
                headers=self._headers(),
                timeout=10,
            )
            if resp.ok:
                return resp.json()[:limit]
        except Exception:
            pass
        return []

    def get_latest_transaction_for_account(self, account_id) -> dict | None:
        txns = self.get_transactions_for_account(account_id, limit=1)
        return txns[0] if txns else None


# ── AccountsRepository ────────────────────────────────────────────────────────
# Thin wrapper so customer_app.py keeps the same interface it always had.

class AccountsRepository:
    """
    Wraps MiddlewareClient with the same method signatures that
    customer_app.py expects from the original local AccountsRepository.
    """

    def __init__(self, middleware_url: str):
        self._client = MiddlewareClient(middleware_url)

    def authenticate_with_status(self, account_number: str, pin: str) -> dict:
        return self._client.authenticate_with_status(account_number, pin)

    def authenticate(self, account_number: str, pin: str):
        return self._client.authenticate(account_number, pin)

    def get_balance(self, account_id: str) -> float:
        return self._client.get_balance()

    def get_account(self, account_id: str) -> dict | None:
        return self._client.get_account()

    @property
    def client(self) -> MiddlewareClient:
        return self._client


# ── TransactionsRepository ────────────────────────────────────────────────────
# All writes happen in middleware / Core Banking. This class only reads.

class TransactionsRepository:
    """Read-only view of transaction history via the middleware."""

    def __init__(self, accounts_repo: AccountsRepository):
        self._accounts_repo = accounts_repo

    @property
    def _client(self) -> MiddlewareClient:
        return self._accounts_repo.client

    def get_transactions_for_account(self, account_id, limit=10) -> list:
        return self._client.get_transactions_for_account(account_id, limit)

    def get_latest_transaction_for_account(self, account_id) -> dict | None:
        return self._client.get_latest_transaction_for_account(account_id)


# ── ATMApp ────────────────────────────────────────────────────────────────────

class ATMApp:
    """
    Layer 1: pure UI orchestration. No banking logic whatsoever.

    Responsibilities:
      - Track current logged-in account
      - Delegate deposit/withdraw to middleware via accounts_repo
      - Return results to Flask UI (customer_app.py)

    NOT responsible for:
      - Balance calculation  → Spring Boot
      - Transaction recording → Core Banking (PostgreSQL)
      - Blockchain logging   → Middleware
      - Atomicity/rollback   → Middleware
      - Pessimistic locking  → Spring Boot
    """

    def __init__(self,
                 accounts_repo: AccountsRepository,
                 transactions_repo: TransactionsRepository):
        self.accounts_repo = accounts_repo
        self.transactions_repo = transactions_repo
        self.current_account = None

    def authenticate(self, account_id: str, pin: str) -> bool:
        account = self.accounts_repo.authenticate(account_id, pin)
        if not account:
            return False
        self.current_account = account["account_id"]
        return True

    def check_balance(self) -> float:
        return self.accounts_repo.get_balance(self.current_account)

    def deposit(self, amount: float, *, idempotency_key: str) -> tuple[bool, str, bool]:
        if amount <= 0:
            return False, "Amount must be positive", False
        ok, msg, result = self.accounts_repo.client.deposit(
            amount, idempotency_key=idempotency_key
        )
        on_chain = bool(result and result.get("blockchainTx")) if ok else False
        return ok, msg, on_chain

    def withdraw(self, amount: float, *, idempotency_key: str) -> tuple[bool, str, bool]:
        if amount <= 0:
            return False, "Amount must be positive", False
        ok, msg, result = self.accounts_repo.client.withdraw(
            amount, idempotency_key=idempotency_key
        )
        on_chain = bool(result and result.get("blockchainTx")) if ok else False
        return ok, msg, on_chain

    def verify_integrity(self, txn) -> tuple[bool, str]:
        """
        Integrity is verified by middleware against the blockchain.
        ATM just reads the status from middleware transaction history.
        """
        status = txn.get("status", "unknown")
        if status == "CONFIRMED":
            return True, "Authentic and confirmed"
        if status == "ROLLED_BACK":
            return False, "Transaction was rolled back (atomicity)"
        if status == "FAILED":
            return False, "Transaction failed"
        return False, f"Status: {status}"


# ── Factory ───────────────────────────────────────────────────────────────────

def make_repos() -> tuple[AccountsRepository, TransactionsRepository]:
    print(f"[ATM Layer 1] Middleware at {MIDDLEWARE_URL}")
    accounts_repo = AccountsRepository(MIDDLEWARE_URL)
    transactions_repo = TransactionsRepository(accounts_repo)
    return accounts_repo, transactions_repo
