"""
In-process mocks for MiddlewareClient so UI tests run without Layer 2/3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch


@dataclass
class MockState:
    balance: float = 500.0
    customer_name: str = "Test User"
    account_number: str = "DE89370400440532013000"
    card_number: str = "4111111111111111"
    invalid_pin: str = "0000"
    next_transaction_id: int = field(default=100, repr=False)

    def _next_tx_id(self) -> int:
        self.next_transaction_id += 1
        return self.next_transaction_id


def install_middleware_mocks(state: MockState) -> list:
    """Patch MiddlewareClient methods; return patchers (call .start() / .stop())."""

    def check_account_status(self, card_number: str) -> dict:
        return {"status": "ok", "card_number": card_number}

    def authenticate_with_status(self, card_number: str, pin: str) -> dict:
        if pin == state.invalid_pin:
            return {"status": "invalid", "attempts_to_next_lock": 2}
        self._session_token = "ui-test-session-token"
        self._customer_name = state.customer_name
        self._account_number = state.account_number
        self._cached_balance = float(state.balance)
        return {
            "status": "ok",
            "accountNumber": state.account_number,
            "account": {
                "account_id": state.account_number,
                "name": state.customer_name,
                "balance": state.balance,
            },
        }

    def deposit(self, amount: float, *, idempotency_key: str):
        state.balance = float(state.balance) + float(amount)
        self._cached_balance = state.balance
        tx_id = state._next_tx_id()
        return True, f"DEPOSIT ${amount:.2f}", {
            "newBalance": state.balance,
            "blockchainTx": None,
            "transactionId": tx_id,
        }

    def withdraw(self, amount: float, *, idempotency_key: str):
        if float(amount) > float(state.balance):
            return False, "Insufficient funds", None
        state.balance = float(state.balance) - float(amount)
        self._cached_balance = state.balance
        tx_id = state._next_tx_id()
        return True, f"WITHDRAW ${amount:.2f}", {
            "newBalance": state.balance,
            "middlewareTxId": tx_id,
            "transactionId": tx_id,
        }

    def confirm_dispense(self, middleware_tx_id: int):
        return True, "Cash dispense confirmed"

    def get_transactions_for_account(self, account_id, limit=None):
        return []

    def get_latest_transaction_for_account(self, account_id):
        return None

    targets = [
        ("atm_architecture.MiddlewareClient.check_account_status", check_account_status),
        ("atm_architecture.MiddlewareClient.authenticate_with_status", authenticate_with_status),
        ("atm_architecture.MiddlewareClient.deposit", deposit),
        ("atm_architecture.MiddlewareClient.withdraw", withdraw),
        ("atm_architecture.MiddlewareClient.confirm_dispense", confirm_dispense),
        ("atm_architecture.MiddlewareClient.get_transactions_for_account", get_transactions_for_account),
        ("atm_architecture.MiddlewareClient.get_latest_transaction_for_account", get_latest_transaction_for_account),
    ]
    return [patch(target, fn) for target, fn in targets]
