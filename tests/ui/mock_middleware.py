"""
In-process mocks for MiddlewareClient so UI tests run without Layer 2/3.

Supports progressive lockout simulation (3 failures → timed lock).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from unittest.mock import patch


@dataclass
class MockState:
    balance: float = 500.0
    customer_name: str = "Test User"
    account_number: str = "DE89370400440532013000"
    card_number: str = "4111111111111111"
    valid_pin: str = "1234"
    lockout_threshold: int = 3
    lock_duration_seconds: int = 5
    next_transaction_id: int = field(default=100, repr=False)
    _failures: dict[str, int] = field(default_factory=dict, repr=False)
    _locked_until: dict[str, float] = field(default_factory=dict, repr=False)

    def reset_lockouts(self) -> None:
        self._failures.clear()
        self._locked_until.clear()

    def lock_card(self, card_number: str, seconds: int | None = None) -> None:
        """Force a timed lock (e.g. for check-account-before-PIN tests)."""
        duration = seconds if seconds is not None else self.lock_duration_seconds
        self._locked_until[card_number] = time.time() + duration
        self._failures[card_number] = self.lockout_threshold

    def is_locked(self, card_number: str) -> bool:
        until = self._locked_until.get(card_number)
        if until is None:
            return False
        if time.time() >= until:
            self._locked_until.pop(card_number, None)
            self._failures.pop(card_number, None)
            return False
        return True

    def remaining_lock_seconds(self, card_number: str) -> int:
        until = self._locked_until.get(card_number)
        if until is None:
            return 0
        return max(0, int(until - time.time()))

    def _record_failure(self, card_number: str) -> int:
        count = self._failures.get(card_number, 0) + 1
        self._failures[card_number] = count
        if count >= self.lockout_threshold and count % self.lockout_threshold == 0:
            self._locked_until[card_number] = time.time() + self.lock_duration_seconds
        return count

    def _next_tx_id(self) -> int:
        self.next_transaction_id += 1
        return self.next_transaction_id


def install_middleware_mocks(state: MockState) -> list:
    """Patch MiddlewareClient methods; return patchers (call .start() / .stop())."""

    def _locked_payload(card_number: str) -> dict:
        return {
            "status": "locked",
            "remaining_lock_seconds": state.remaining_lock_seconds(card_number),
            "lock_minutes": max(1, state.remaining_lock_seconds(card_number) // 60),
        }

    def check_account_status(self, card_number: str) -> dict:
        if state.is_locked(card_number):
            return _locked_payload(card_number)
        return {"status": "ok", "card_number": card_number}

    def authenticate_with_status(self, card_number: str, pin: str) -> dict:
        if state.is_locked(card_number):
            return _locked_payload(card_number)

        if pin != state.valid_pin:
            failures = state._record_failure(card_number)
            if state.is_locked(card_number):
                return _locked_payload(card_number)
            attempts_left = state.lockout_threshold - (failures % state.lockout_threshold)
            if attempts_left == 0:
                attempts_left = state.lockout_threshold
            return {
                "status": "invalid",
                "attempts_to_next_lock": attempts_left,
            }

        state._failures.pop(card_number, None)
        state._locked_until.pop(card_number, None)
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
