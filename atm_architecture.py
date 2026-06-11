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

import mw_http

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

MIDDLEWARE_URL = os.environ.get("MIDDLEWARE_URL", "https://mw.local").rstrip("/")


def _mw_unreachable(exc: Exception) -> RuntimeError:
    if isinstance(exc, RuntimeError):
        return exc
    if isinstance(exc, requests.exceptions.SSLError):
        return RuntimeError(
            f"TLS/mTLS failed for middleware at {MIDDLEWARE_URL}.\n"
            "Run: ./scripts/gen_kiosk_client_cert.sh\n"
            "Ensure Caddy mw.local has client_auth (scripts/caddy/Caddyfile.example)\n"
            "Then: cd ~/atm-tls && sudo caddy run --config Caddyfile"
        )
    return RuntimeError(
        f"Cannot reach middleware at {MIDDLEWARE_URL}.\n"
        "Start it: cd atm-middleware && python3 middleware.py\n"
        "And Caddy: cd ~/atm-tls && sudo caddy run --config Caddyfile"
    )


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

    def check_account_status(self, card_number: str) -> dict:
        """POST /atm/account-status — lockout / PIN-reset state without a PIN.
        Middleware resolves card_number → account_number internally before
        checking lockout state, so the lockout is always keyed by accountNumber.
        """
        try:
            resp = mw_http.post(
                f"{self.base_url}/atm/account-status",
                json={"cardNumber": card_number},   # middleware expects cardNumber
                headers={"X-Channel": "ATM_WEB"},
                timeout=60,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError, RuntimeError) as e:
            raise _mw_unreachable(e) from e
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(
                f"Middleware returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            )

    def authenticate_with_status(self, card_number: str, pin: str) -> dict:
        """
        POST /atm/login → middleware → Spring Boot /atm/login.
        Spring Boot verifies BCrypt PIN via card number and returns a JWT.
        Middleware creates a session token and returns it to ATM.
        Lockouts are keyed by accountNumber (resolved inside middleware).
        """
        try:
            resp = mw_http.post(
                f"{self.base_url}/atm/login",
                json={"cardNumber": card_number, "pin": pin},   # middleware expects cardNumber
                headers={"X-Channel": "ATM_WEB"},
                timeout=60,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError, RuntimeError) as e:
            raise _mw_unreachable(e) from e

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"Middleware returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        status = data.get("status", "invalid")

        if status == "pin_reset_required":
            return {
                "status": "pin_reset_required",
                "accountNumber": data.get("accountNumber", ""),
            }

        if status == "locked":
            out = {
                "status": "locked",
                "remaining_lock_seconds": data.get("remaining_lock_seconds", 300),
                "lock_minutes": data.get("lock_minutes"),
                "message": data.get("message"),
            }
            if data.get("admin_unlock_required"):
                out["admin_unlock_required"] = True
            if data.get("terminal_lock"):
                out["terminal_lock"] = True
            return out

        if status != "ok":
            return {
                "status": "invalid",
                "attempts_to_next_lock": data.get("attempts_to_next_lock"),
            }

        self._session_token = data["sessionToken"]
        self._customer_name = data.get("customerName", "Customer")
        self._account_number = data.get("accountNumber", card_number)
        self._cached_balance = float(data.get("balance", 0))

        return {
            "status": "ok",
            "accountNumber": self._account_number,
            "fingerprintSlotId": data.get("fingerprintSlotId"),
            "account": {
                "account_id": self._account_number,
                "name": self._customer_name,
                "balance": self._cached_balance,
            },
        }

    def authenticate(self, card_number: str, pin: str):
        result = self.authenticate_with_status(card_number, pin)
        if result.get("status") != "ok":
            return None
        return result.get("account")

    def reset_pin(
        self,
        new_pin: str,
        confirm_pin: str,
        *,
        card_number: str | None = None,
        account_number: str | None = None,
    ) -> dict:
        """POST /atm/reset-pin — customer sets new PIN after admin unlock."""
        payload: dict = {"newPin": new_pin, "confirmPin": confirm_pin}
        if card_number:
            payload["cardNumber"] = card_number.replace(" ", "")
        if account_number:
            payload["accountNumber"] = account_number.strip()
        if not payload.get("cardNumber") and not payload.get("accountNumber"):
            return {"status": "error", "message": "Card or account number is required."}

        try:
            resp = mw_http.post(
                f"{self.base_url}/atm/reset-pin",
                json=payload,
                headers={"X-Channel": "ATM_WEB"},
                timeout=60,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError, RuntimeError) as e:
            raise _mw_unreachable(e) from e
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(
                f"Middleware returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        if resp.status_code >= 400 and body.get("status") != "error":
            detail = body.get("detail") or body.get("error") or resp.text
            return {"status": "error", "message": detail}
        return body

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
            resp = mw_http.post(
                f"{self.base_url}/atm/deposit",
                json={"amount": amount},
                headers=self._mutation_headers(idempotency_key),
                timeout=30,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError):
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
        Core Banking tracks dispense state; call confirm_dispense (via /atm/ack)
        after cash is dispensed or the bank scheduler auto-reverses.
        """
        try:
            resp = mw_http.post(
                f"{self.base_url}/atm/withdraw",
                json={"amount": amount},
                headers=self._mutation_headers(idempotency_key),
                timeout=30,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError):
            return False, "Cannot reach middleware", None

        if resp.status_code == 400:
            return False, "Insufficient funds", None
        if not resp.ok:
            try:
                err = resp.json()
                detail = err.get("detail", resp.text)
                if isinstance(detail, list):
                    detail = "; ".join(
                        str(x.get("msg", x)) for x in detail if isinstance(x, dict)
                    ) or resp.text
            except Exception:
                detail = resp.text
            return False, f"Withdraw failed: {detail}", None

        data = resp.json()
        self._cached_balance = float(data.get("newBalance", self._cached_balance))

        msg = f"WITHDRAW ${amount:.2f}. New balance: ${self._cached_balance:.2f}"
        if not data.get("blockchainTx"):
            msg += " Recorded locally; blockchain sync will retry shortly."

        return True, msg, data

    def confirm_dispense(self, middleware_tx_id: int) -> tuple[bool, str]:
        """POST /atm/ack → Core Banking confirm-dispense after cash is dispensed."""
        try:
            resp = mw_http.post(
                f"{self.base_url}/atm/ack",
                json={"middlewareTxId": middleware_tx_id},
                headers=self._headers(),
                timeout=60,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError):
            return False, "Cannot reach middleware"
        if not resp.ok:
            return False, f"Dispense confirm failed: {resp.text}"
        return True, "Cash dispense confirmed"

    # ── Transaction history ───────────────────────────────────────────────────

    def get_transactions_for_account(self, account_id, limit=None) -> list:
        """GET /atm/transactions → middleware proxies to Core Banking. Returns all transactions."""
        try:
            resp = mw_http.get(
                f"{self.base_url}/atm/transactions",
                headers=self._headers(),
                timeout=60,
            )
            if resp.ok:
                data = resp.json()
                return data[:limit] if limit else data
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
    Parameter names updated to card_number to match the new card-based login flow.
    """

    def __init__(self, middleware_url: str):
        self._client = MiddlewareClient(middleware_url)

    def check_account_status(self, card_number: str) -> dict:
        return self._client.check_account_status(card_number)

    def authenticate_with_status(self, card_number: str, pin: str) -> dict:
        return self._client.authenticate_with_status(card_number, pin)

    def authenticate(self, card_number: str, pin: str):
        return self._client.authenticate(card_number, pin)

    def reset_pin(
        self,
        new_pin: str,
        confirm_pin: str,
        *,
        card_number: str | None = None,
        account_number: str | None = None,
    ) -> dict:
        return self._client.reset_pin(
            new_pin,
            confirm_pin,
            card_number=card_number,
            account_number=account_number,
        )

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

    def get_transactions_for_account(self, account_id, limit=None) -> list:
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

    def authenticate(self, card_number: str, pin: str) -> bool:
        account = self.accounts_repo.authenticate(card_number, pin)
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