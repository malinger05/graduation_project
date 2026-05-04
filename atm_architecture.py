"""
atm_architecture.py  —  Layer 1
ATM talks ONLY to middleware (Layer 2).
Never calls Spring Boot directly.
No blockchain code here — middleware handles that.
"""

import csv
import inspect
import os
import time
import threading
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from secrets_manager import get_secret

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

# Layer 2 middleware URL
MIDDLEWARE_URL = os.environ.get("MIDDLEWARE_URL", "http://localhost:8000").rstrip("/")

# Local PostgreSQL for ATM transaction log (read-only display, written by middleware)
DATABASE_URL = get_secret("DATABASE_URL", "postgresql://localhost:5432/atm").strip()

INDEXER_INTERVAL_SECONDS = float(os.environ.get("INDEXER_INTERVAL_SECONDS", "3").strip())


# ── Middleware client ─────────────────────────────────────────────────────────

class MiddlewareClient:
    """
    All ATM operations go through here → Layer 2 middleware.
    The ATM holds a session token returned at login.
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

    def authenticate(self, account_number: str, pin: str) -> dict | None:
        """POST /atm/login → middleware → Spring Boot"""
        try:
            resp = requests.post(
                f"{self.base_url}/atm/login",
                json={"accountNumber": account_number, "pin": pin},
                timeout=10,
            )
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Cannot reach middleware at {self.base_url}.\n"
                "Is middleware running? → python3 middleware.py"
            )

        if resp.status_code == 401:
            return None
        if not resp.ok:
            raise RuntimeError(f"Middleware error {resp.status_code}: {resp.text}")

        data = resp.json()
        self._session_token = data["sessionToken"]
        self._customer_name = data.get("customerName", "Customer")
        self._account_number = account_number
        self._cached_balance = float(data.get("balance", 0))

        return {
            "account_id": account_number,
            "name": self._customer_name,
        }

    def get_balance(self, account_id: str = None) -> float:
        return self._cached_balance

    def get_account(self, account_id: str = None) -> dict | None:
        if not self._session_token:
            return None
        return {
            "account_id": self._account_number,
            "name": self._customer_name,
            "balance": self._cached_balance,
        }

    def deposit(self, amount: float) -> dict:
        """POST /atm/deposit → middleware records PENDING → calls Spring Boot → records result"""
        resp = requests.post(
            f"{self.base_url}/atm/deposit",
            json={"amount": amount},
            headers=self._headers(),
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"Deposit failed: {resp.text}")
        data = resp.json()
        self._cached_balance = float(data["newBalance"])
        return data

    def withdraw(self, amount: float) -> dict:
        """
        POST /atm/withdraw → middleware records PENDING → calls Spring Boot →
        records SUCCESS → ATM must call ack() after dispensing cash
        """
        resp = requests.post(
            f"{self.base_url}/atm/withdraw",
            json={"amount": amount},
            headers=self._headers(),
            timeout=30,
        )
        if resp.status_code == 400:
            raise RuntimeError("Insufficient funds")
        if not resp.ok:
            raise RuntimeError(f"Withdraw failed: {resp.text}")
        data = resp.json()
        self._cached_balance = float(data["newBalance"])
        return data

    def ack(self, middleware_tx_id: int):
        """
        Call this after cash is dispensed.
        Middleware marks transaction CONFIRMED and cancels rollback timer.
        """
        resp = requests.post(
            f"{self.base_url}/atm/ack",
            json={"middlewareTxId": middleware_tx_id},
            headers=self._headers(),
            timeout=10,
        )
        return resp.ok

    def get_transactions(self) -> list:
        resp = requests.get(
            f"{self.base_url}/atm/transactions",
            headers=self._headers(),
            timeout=10,
        )
        if resp.ok:
            return resp.json()
        return []


# ── AccountsRepository wrapper (keeps customer_app.py interface happy) ────────

class AccountsRepository:
    """
    Thin wrapper around MiddlewareClient so customer_app.py
    doesn't need to change its interface.
    """

    def __init__(self, middleware_url: str):
        self._client = MiddlewareClient(middleware_url)

    def authenticate(self, account_number: str, pin: str):
        return self._client.authenticate(account_number, pin)

    def get_balance(self, account_id: str) -> float:
        return self._client.get_balance()

    def get_account(self, account_id: str):
        return self._client.get_account()

    @property
    def client(self) -> MiddlewareClient:
        return self._client


def make_accounts_repo():
    print(f"[ATM Layer 1] Connecting to middleware at {MIDDLEWARE_URL}")
    return AccountsRepository(MIDDLEWARE_URL)


# ── ATMApp ────────────────────────────────────────────────────────────────────

class ATMApp:
    """
    Layer 1: pure UI logic.
    Delegates ALL banking operations to middleware via AccountsRepository.
    No blockchain code. No balance calculations. No PostgreSQL writes.
    """

    def __init__(self, accounts_repo: AccountsRepository):
        self.accounts_repo = accounts_repo
        self.current_account = None

    def authenticate(self, account_id: str, pin: str) -> bool:
        account = self.accounts_repo.authenticate(account_id, pin)
        if not account:
            return False
        self.current_account = account["account_id"]
        print(f"Welcome {account['name']}")
        return True

    def check_balance(self) -> float:
        return self.accounts_repo.get_balance(self.current_account)

    def deposit(self, amount: float) -> tuple[bool, str, dict | None]:
        if amount <= 0:
            return False, "Amount must be positive", None
        try:
            result = self.accounts_repo.client.deposit(amount)
            msg = f"DEPOSIT ${amount:.2f}. New balance: ${result['newBalance']:.2f}"
            return True, msg, result
        except RuntimeError as e:
            return False, str(e), None

    def withdraw(self, amount: float) -> tuple[bool, str, dict | None]:
        if amount <= 0:
            return False, "Amount must be positive", None
        try:
            result = self.accounts_repo.client.withdraw(amount)
            # Simulate cash dispensed → send ACK to middleware
            # In real ATM this happens after physical cash dispense
            mid_tx_id = result.get("middlewareTxId")
            if mid_tx_id:
                self.accounts_repo.client.ack(mid_tx_id)
            msg = f"WITHDRAW ${amount:.2f}. New balance: ${result['newBalance']:.2f}"
            return True, msg, result
        except RuntimeError as e:
            return False, str(e), None

    def get_transactions(self) -> list:
        return self.accounts_repo.client.get_transactions()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    repo = make_accounts_repo()
    atm = ATMApp(repo)

    while True:
        print("\n1. Login\n2. Exit")
        choice = input("Choose: ").strip()
        if choice == "1":
            account = input("Account number: ").strip()
            pin = input("PIN: ").strip()
            try:
                if not atm.authenticate(account, pin):
                    print("Invalid credentials")
                    continue
            except RuntimeError as e:
                print(f"Error: {e}")
                continue

            while True:
                print(
                    f"\nBalance: ${atm.check_balance():.2f}\n"
                    "1. Withdraw\n2. Deposit\n"
                    "3. Transaction History\n4. Logout"
                )
                action = input("Choose: ").strip()
                if action == "1":
                    try:
                        amount = float(input("Withdraw amount: $"))
                        ok, msg, _ = atm.withdraw(amount)
                        print(msg)
                    except ValueError:
                        print("Invalid amount")
                elif action == "2":
                    try:
                        amount = float(input("Deposit amount: $"))
                        ok, msg, _ = atm.deposit(amount)
                        print(msg)
                    except ValueError:
                        print("Invalid amount")
                elif action == "3":
                    txns = atm.get_transactions()
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