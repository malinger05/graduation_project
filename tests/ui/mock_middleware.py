"""
In-process mocks for MiddlewareClient and mw_http (card setup) — no Layer 2/3.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch


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
    # Per-card overrides: ok | pin_reset_required | admin_locked | timed_locked
    account_mode: dict[str, str] = field(default_factory=dict, repr=False)
    transactions: list[dict] = field(default_factory=list, repr=False)
    deposit_include_blockchain: bool = False
    card_setup_card_id: int = 9001

    def reset_lockouts(self) -> None:
        self._failures.clear()
        self._locked_until.clear()
        self.account_mode.clear()

    def reset_all(self) -> None:
        self.reset_lockouts()
        self.balance = 500.0
        self.valid_pin = "1234"
        self.next_transaction_id = 100
        self.transactions = []
        self.deposit_include_blockchain = False
        self.account_mode.clear()

    def set_pin_reset_required(self, card_number: str | None = None) -> None:
        self.account_mode[card_number or self.card_number] = "pin_reset_required"

    def set_admin_locked(self, card_number: str | None = None) -> None:
        self.account_mode[card_number or self.card_number] = "admin_locked"

    def lock_card(self, card_number: str, seconds: int | None = None) -> None:
        duration = seconds if seconds is not None else self.lock_duration_seconds
        self._locked_until[card_number] = time.time() + duration
        self._failures[card_number] = self.lockout_threshold
        self.account_mode[card_number] = "timed_locked"

    def is_locked(self, card_number: str) -> bool:
        until = self._locked_until.get(card_number)
        if until is None:
            return False
        if time.time() >= until:
            self._locked_until.pop(card_number, None)
            self._failures.pop(card_number, None)
            if self.account_mode.get(card_number) == "timed_locked":
                self.account_mode.pop(card_number, None)
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

    def default_transactions(self) -> list[dict]:
        return [
            {
                "type": "DEPOSIT",
                "amount": 50.0,
                "timestamp": "2026-06-01T10:00:00",
                "chainStatus": "CONFIRMED",
            },
            {
                "type": "WITHDRAW",
                "amount": 20.0,
                "timestamp": "2026-06-01T09:00:00",
                "chainStatus": "PENDING_SUBMIT",
            },
        ]


class _MockHttpResponse:
    def __init__(self, status_code: int, payload: dict | list | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")
        self.ok = 200 <= status_code < 300

    def json(self) -> Any:
        return self._payload if self._payload is not None else {}


def install_middleware_mocks(state: MockState) -> list:
    """Patch MiddlewareClient methods; return patchers (call .start() / .stop())."""

    def _mode(card_number: str) -> str | None:
        return state.account_mode.get(card_number)

    def _locked_payload(card_number: str) -> dict:
        return {
            "status": "locked",
            "remaining_lock_seconds": state.remaining_lock_seconds(card_number),
            "lock_minutes": max(1, state.remaining_lock_seconds(card_number) // 60),
        }

    def _admin_locked_payload(card_number: str) -> dict:
        return {
            "status": "locked",
            "admin_unlock_required": True,
            "remaining_lock_seconds": 0,
        }

    def check_account_status(self, card_number: str) -> dict:
        mode = _mode(card_number)
        if mode == "pin_reset_required":
            return {"status": "pin_reset_required", "accountNumber": state.account_number}
        if mode == "admin_locked":
            return _admin_locked_payload(card_number)
        if mode == "timed_locked" or state.is_locked(card_number):
            return _locked_payload(card_number)
        return {"status": "ok", "card_number": card_number}

    def authenticate_with_status(self, card_number: str, pin: str) -> dict:
        mode = _mode(card_number)
        if mode == "pin_reset_required":
            return {"status": "pin_reset_required", "accountNumber": state.account_number}
        if mode == "admin_locked":
            return _admin_locked_payload(card_number)
        if mode == "timed_locked" or state.is_locked(card_number):
            return _locked_payload(card_number)

        if pin != state.valid_pin:
            failures = state._record_failure(card_number)
            if state.is_locked(card_number):
                return _locked_payload(card_number)
            attempts_left = state.lockout_threshold - (failures % state.lockout_threshold)
            if attempts_left == 0:
                attempts_left = state.lockout_threshold
            return {"status": "invalid", "attempts_to_next_lock": attempts_left}

        state._failures.pop(card_number, None)
        state._locked_until.pop(card_number, None)
        state.account_mode.pop(card_number, None)
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

    def reset_pin(self, card_number: str, new_pin: str, confirm_pin: str) -> dict:
        if new_pin != confirm_pin:
            return {"status": "error", "message": "PINs do not match."}
        if len(new_pin) != 4 or not new_pin.isdigit():
            return {"status": "error", "message": "PIN must be 4 digits."}
        state.valid_pin = new_pin
        state.account_mode.pop(card_number, None)
        return {"status": "ok", "message": "PIN updated. Please log in with your new PIN."}

    def deposit(self, amount: float, *, idempotency_key: str):
        state.balance = float(state.balance) + float(amount)
        self._cached_balance = state.balance
        tx_id = state._next_tx_id()
        payload = {
            "newBalance": state.balance,
            "transactionId": tx_id,
        }
        if state.deposit_include_blockchain:
            payload["blockchainTx"] = "0x" + "a" * 64
            payload["verifyUrl"] = f"https://sepolia.etherscan.io/tx/{payload['blockchainTx']}"
        else:
            payload["blockchainTx"] = None
        return True, f"DEPOSIT ${amount:.2f}", payload

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
        rows = state.transactions or state.default_transactions()
        return rows[:limit] if limit else rows

    def get_latest_transaction_for_account(self, account_id):
        rows = state.transactions or state.default_transactions()
        return rows[0] if rows else None

    def mw_http_post(url: str, **kwargs) -> _MockHttpResponse:
        url = str(url)
        if "/atm/prepare-own-pin" in url:
            body = kwargs.get("json") or {}
            acct = (body.get("accountNumber") or state.account_number).upper()
            return _MockHttpResponse(
                201,
                {
                    "cardId": state.card_setup_card_id,
                    "accountNumber": acct,
                    "maskedNumber": "**** **** **** 1111",
                    "holderInitials": "TU",
                    "hadExistingCards": False,
                },
            )
        if "/atm/set-own-pin" in url:
            return _MockHttpResponse(
                200,
                {"maskedNumber": "**** **** **** 1111", "message": "PIN set"},
            )
        return _MockHttpResponse(503, {"error": f"Unmocked POST {url}"})

    def mw_http_get(url: str, **kwargs) -> _MockHttpResponse:
        return _MockHttpResponse(503, {"error": f"Unmocked GET {url}"})

    targets = [
        ("atm_architecture.MiddlewareClient.check_account_status", check_account_status),
        ("atm_architecture.MiddlewareClient.authenticate_with_status", authenticate_with_status),
        ("atm_architecture.MiddlewareClient.reset_pin", reset_pin),
        ("atm_architecture.MiddlewareClient.deposit", deposit),
        ("atm_architecture.MiddlewareClient.withdraw", withdraw),
        ("atm_architecture.MiddlewareClient.confirm_dispense", confirm_dispense),
        ("atm_architecture.MiddlewareClient.get_transactions_for_account", get_transactions_for_account),
        ("atm_architecture.MiddlewareClient.get_latest_transaction_for_account", get_latest_transaction_for_account),
        ("mw_http.post", mw_http_post),
        ("customer_app.mw_http.post", mw_http_post),
    ]
    return [patch(target, fn) for target, fn in targets]
