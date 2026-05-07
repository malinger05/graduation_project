"""
atm_architecture.py  —  Layer 1
ATM is a pure UI client. Zero banking logic here.

WHERE EACH RESPONSIBILITY LIVES:
  Layer 1 (this file)   → show UI, send requests, display results
  Layer 2 (middleware)  → record transactions, atomicity, blockchain logging
  Layer 3 (Spring Boot) → deposit, withdraw, balance, customer, account data
"""

import os
import time
from dotenv import load_dotenv
from secrets_manager import get_secret
import requests

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

MIDDLEWARE_URL = os.environ.get("MIDDLEWARE_URL", "http://localhost:8000").rstrip("/")

# Kept so existing imports (worker.py etc.) don't break
DATABASE_URL = get_secret("DATABASE_URL", "postgresql://localhost:5432/atm").strip()
ACCOUNTS_DATABASE_URL = get_secret("ACCOUNTS_DATABASE_URL", DATABASE_URL).strip()
ACCOUNTS_TABLE = os.environ.get("ACCOUNTS_TABLE", "accounts").strip()
INDEXER_INTERVAL_SECONDS = float(os.environ.get("INDEXER_INTERVAL_SECONDS", "3").strip())


# ── Middleware HTTP client ─────────────────────────────────────────────────────

class MiddlewareClient:
    """
    Single HTTP client for all ATM operations.
    Holds session token returned at login.
    Never touches PostgreSQL or blockchain directly.
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
        return {"x-session-token": self._session_token}

    # ── Auth ──────────────────────────────────────────────────────────────────

    def authenticate_with_status(self, account_number: str, pin: str) -> dict:
        """
        POST /atm/login → middleware → Spring Boot /atm/login.
        Spring Boot verifies BCrypt PIN and returns JWT.
        Middleware creates a session token and returns it to ATM.
        Returns same shape as original AccountsRepository.authenticate_with_status().
        """
        try:
            resp = requests.post(
                f"{self.base_url}/atm/login",
                json={"accountNumber": account_number, "pin": pin},
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
            }
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

    def deposit(self, amount: float) -> tuple[bool, str, dict | None]:
        """
        POST /atm/deposit → middleware.

        Middleware (Layer 2) does:
          1. INSERT middleware_transactions status=PENDING
          2. POST /accounts/{id}/deposit → Spring Boot (Layer 3)
          3. Spring Boot: acquires pessimistic lock, adds balance, saves to PostgreSQL
          4. UPDATE status=SUCCESS
          5. Writes canonical hash to Sepolia blockchain

        ATM just shows the result.
        """
        try:
            resp = requests.post(
                f"{self.base_url}/atm/deposit",
                json={"amount": amount},
                headers=self._headers(),
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

    def withdraw(self, amount: float) -> tuple[bool, str, dict | None]:
        """
        POST /atm/withdraw → middleware.

        Middleware (Layer 2) does:
          1. INSERT middleware_transactions status=PENDING
          2. POST /accounts/{id}/withdraw → Spring Boot (Layer 3)
          3. Spring Boot: acquires pessimistic lock, subtracts balance, saves to PostgreSQL
          4. UPDATE status=SUCCESS, starts 30s ACK timer
          5. ATM sends /atm/ack after cash is dispensed
          6. No ACK in 30s → middleware reverses debit on Spring Boot (atomicity rollback)
        """
        try:
            resp = requests.post(
                f"{self.base_url}/atm/withdraw",
                json={"amount": amount},
                headers=self._headers(),
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

        # Send ACK — simulates physical cash dispense confirmation
        # On real ATM hardware this fires AFTER the cash drawer opens
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
                # No ACK → atomicity monitor will automatically reverse the debit
                pass

        msg = f"WITHDRAW ${amount:.2f}. New balance: ${self._cached_balance:.2f}"
        if not data.get("blockchainTx"):
            msg += " Recorded locally; blockchain sync will retry shortly."

        return True, msg, data

    # ── Transaction history ───────────────────────────────────────────────────

    def get_transactions_for_account(self, account_id, limit=10) -> list:
        """GET /atm/transactions → middleware reads from middleware_transactions table."""
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
# Thin wrapper so customer_app.py keeps the same interface it always had

class AccountsRepository:
    """
    Wraps MiddlewareClient with the same method signatures
    that customer_app.py expects from the original local AccountsRepository.
    """

    # Sentinel so any DSN check in calling code passes
    class _FakeConn:
        dsn = "__middleware__"
    conn = _FakeConn()
    table_name = "accounts"

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
# All writes happen in middleware. This class only reads.

class TransactionsRepository:
    """
    In 3-layer mode all transaction writes happen in middleware (Layer 2).
    This class provides read access to middleware transaction history via HTTP.
    """

    class _FakeConn:
        dsn = "__middleware__"
    conn = _FakeConn()

    def __init__(self, accounts_repo: AccountsRepository):
        self._accounts_repo = accounts_repo

    @property
    def _client(self) -> MiddlewareClient:
        return self._accounts_repo.client

    def get_transactions_for_account(self, account_id, limit=10) -> list:
        return self._client.get_transactions_for_account(account_id, limit)

    def get_latest_transaction_for_account(self, account_id) -> dict | None:
        return self._client.get_latest_transaction_for_account(account_id)

    # Stubs — all these operations happen in middleware or Spring Boot
    def get_all_transactions(self): return []
    def get_transaction_by_id(self, tx_id): return None
    def get_pending_transactions(self): return []
    def get_confirmed_transactions(self): return []
    def get_transactions_for_submission(self, **kwargs): return []
    def create_local_transaction_atomic(self, *args, **kwargs):
        raise NotImplementedError("Transactions handled by middleware in 3-layer mode.")


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
      - Transaction recording → Middleware
      - Blockchain logging   → Middleware
      - Atomicity/rollback   → Middleware
      - Pessimistic locking  → Spring Boot
    """

    def __init__(self, accounts_repo: AccountsRepository,
                 transactions_repo: TransactionsRepository,
                 blockchain=None,   # unused in 3-layer mode
                 indexer=None):     # unused in 3-layer mode
        self.accounts_repo = accounts_repo
        self.transactions_repo = transactions_repo
        self.blockchain = blockchain
        self.indexer = indexer
        self.current_account = None

    def authenticate(self, account_id: str, pin: str) -> bool:
        account = self.accounts_repo.authenticate(account_id, pin)
        if not account:
            return False
        self.current_account = account["account_id"]
        return True

    def check_balance(self) -> float:
        return self.accounts_repo.get_balance(self.current_account)

    def deposit(self, amount: float) -> tuple[bool, str, bool]:
        if amount <= 0:
            return False, "Amount must be positive", False
        ok, msg, result = self.accounts_repo.client.deposit(amount)
        on_chain = bool(result and result.get("blockchainTx")) if ok else False
        return ok, msg, on_chain

    def withdraw(self, amount: float) -> tuple[bool, str, bool]:
        if amount <= 0:
            return False, "Amount must be positive", False
        ok, msg, result = self.accounts_repo.client.withdraw(amount)
        on_chain = bool(result and result.get("blockchainTx")) if ok else False
        return ok, msg, on_chain

    def verify_integrity(self, txn) -> tuple[bool, str]:
        """
        Integrity is verified by middleware indexer against blockchain.
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

    def export_audit_report(self):
        """Audit log lives in middleware DB, not ATM."""
        print("Audit log is in middleware DB:")
        print("psql -d middleware -c 'SELECT * FROM middleware_transactions ORDER BY id DESC;'")


# ── Stub classes for backward compatibility ───────────────────────────────────
# worker.py imports BlockchainGateway and Indexer from here.
# In 3-layer mode these are not used — middleware handles blockchain.

class BlockchainGateway:
    def __init__(self):
        raise RuntimeError(
            "BlockchainGateway is not used in 3-layer mode.\n"
            "Blockchain logging is handled by middleware (Layer 2).\n"
            "Run worker.py only in local mode."
        )


class Indexer:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "Indexer is not used in 3-layer mode.\n"
            "The middleware has its own blockchain retry worker.\n"
            "Run worker.py only in local mode."
        )


# ── Factory ───────────────────────────────────────────────────────────────────

def make_repos():
    """Returns (accounts_repo, transactions_repo, None, None) for 3-layer mode."""
    print(f"[ATM Layer 1] Middleware at {MIDDLEWARE_URL}")
    accounts_repo = AccountsRepository(MIDDLEWARE_URL)
    transactions_repo = TransactionsRepository(accounts_repo)
    return accounts_repo, transactions_repo, None, None


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    accounts_repo, transactions_repo, _, _ = make_repos()
    atm = ATMApp(accounts_repo, transactions_repo)

    while True:
        print("\n1. Login\n2. Exit")
        choice = input("Choose: ").strip()
        if choice == "1":
            account = input("Account number: ").strip()
            pin = input("PIN: ").strip()

            try:
                result = accounts_repo.authenticate_with_status(account, pin)
            except RuntimeError as e:
                print(f"Error: {e}")
                continue

            if result["status"] == "locked":
                secs = result.get("remaining_lock_seconds", 0)
                print(f"Account locked. Try again in {secs // 60}m {secs % 60}s")
                continue
            if result["status"] != "ok":
                left = result.get("attempts_to_next_lock")
                msg = (f"Invalid credentials. {left} attempt(s) left before lockout."
                       if left else "Invalid credentials.")
                print(msg)
                continue

            atm.current_account = account
            print(f"Welcome {result['account']['name']}")

            while True:
                print(
                    f"\nBalance: ${atm.check_balance():.2f}\n"
                    "1. Withdraw\n2. Deposit\n3. Transaction History\n4. Logout"
                )
                action = input("Choose: ").strip()
                if action == "1":
                    try:
                        amount = float(input("Withdraw amount: $"))
                        _, msg, _ = atm.withdraw(amount)
                        print(msg)
                    except ValueError:
                        print("Invalid amount")
                elif action == "2":
                    try:
                        amount = float(input("Deposit amount: $"))
                        _, msg, _ = atm.deposit(amount)
                        print(msg)
                    except ValueError:
                        print("Invalid amount")
                elif action == "3":
                    txns = transactions_repo.get_transactions_for_account(account, 20)
                    if not txns:
                        print("No transactions yet")
                    for t in txns:
                        print(f"  #{t['id']} {t['type']} ${t['amount']} → {t['status']}")
                elif action == "4":
                    break
        elif choice == "2":
            break
        time.sleep(0.1)


if __name__ == "__main__":
    main()