"""
Mocks for admin_app HTTP calls to Core Banking and middleware.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch


@dataclass
class AdminMockState:
    admin_user: str = "admin"
    admin_pass: str = "admin123"
    jwt: str = "ui-test-admin-jwt"
    customers: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    login_should_fail_cb: bool = False

    def reset(self) -> None:
        self.customers = [
            {
                "customerId": 1,
                "firstName": "Jane",
                "lastName": "Doe",
                "email": "jane@example.com",
                "phoneNumber": "+491234",
                "nationalId": "ID123",
                "dateOfBirth": "1990-01-01",
            },
        ]
        self.transactions = [
            {
                "transactionId": 1,
                "chainStatus": "CONFIRMED",
                "amount": 100,
                "balanceAfter": 600,
                "transactionType": "DEPOSIT",
                "accountNumber": "DE89370400440532013000",
                "createdAt": "2026-06-01T10:00:00",
            },
            {
                "transactionId": 2,
                "chainStatus": "PENDING_SUBMIT",
                "amount": 50,
                "balanceAfter": 550,
                "transactionType": "WITHDRAW",
                "accountNumber": "DE89370400440532013000",
                "createdAt": "2026-06-01T09:00:00",
            },
        ]
        self.blocked = [
            {
                "accountNumber": "DE89370400440532013000",
                "customerName": "Jane Doe",
                "lockStatus": "permanently_locked",
            },
        ]
        self.login_should_fail_cb = False


class _Resp:
    def __init__(self, code: int, body: Any = None, text: str = ""):
        self.status_code = code
        self._body = body
        self.text = text
        self.ok = 200 <= code < 300

    def json(self):
        return self._body


def install_admin_mocks(state: AdminMockState) -> list:
    import sys
    from pathlib import Path

    admin_root = Path(__file__).resolve().parents[2] / "admin-app"
    if str(admin_root) not in sys.path:
        sys.path.insert(0, str(admin_root))

    import admin_app

    def _match(method: str, path: str) -> _Resp | None:
        if method == "post" and path == "/auth/login":
            if state.login_should_fail_cb:
                return _Resp(401, {"error": "bad"})
            return _Resp(200, {"token": state.jwt})
        if method == "get" and path == "/customers":
            return _Resp(200, {"content": state.customers})
        if method == "get" and path.startswith("/customers/") and path.endswith("/accounts"):
            return _Resp(
                200,
                [
                    {
                        "accountId": 10,
                        "accountNumber": "DE89370400440532013000",
                        "accountStatus": "ACTIVE",
                        "balance": 500,
                    }
                ],
            )
        if method == "get" and path == "/admin/transactions":
            return _Resp(200, state.transactions)
        if method == "post" and path == "/atm/account-status":
            return _Resp(
                200,
                {
                    "status": "locked",
                    "admin_unlock_required": True,
                    "remaining_lock_seconds": 0,
                },
            )
        if method == "post" and path == "/atm/admin/login-unlock":
            return _Resp(200, {"status": "ok"})
        if method == "post" and path == "/customers":
            return _Resp(201, {"customerId": 99})
        return None

    def _core_call(method: str, path: str, headers=None, timeout=(3, 15), **kwargs):
        r = _match(method, path)
        if r:
            return r
        return _Resp(404, {"error": f"unmocked {method} {path}"})

    def _cb(method, path, **kwargs):
        return _core_call(method, path, headers=admin_app._jwt_headers(), **kwargs)

    def _cb_service(method, path, **kwargs):
        return _core_call(method, path, headers=admin_app._service_headers(), **kwargs)

    def _mw(method, path, **kwargs):
        if method == "post" and path == "/atm/account-status":
            return _core_call("post", path, **kwargs)
        if method == "post" and path == "/atm/admin/login-unlock":
            return _core_call("post", path, **kwargs)
        return None

    return [
        patch.object(admin_app, "_core_call", _core_call),
        patch.object(admin_app, "_cb", _cb),
        patch.object(admin_app, "_cb_service", _cb_service),
        patch.object(admin_app, "_mw", _mw),
    ]
