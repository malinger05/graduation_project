"""Integration tests for middleware API flows (in-process FastAPI)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

import middleware


@dataclass
class _FakeResponse:
    status_code: int
    _payload: dict[str, Any]
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def client(middleware_db, monkeypatch):
    monkeypatch.setattr(middleware.db, "init_db", lambda: True)
    monkeypatch.setattr(middleware.blockchain_worker, "start", lambda *args, **kwargs: None)
    with TestClient(middleware.app) as c:
        yield c


@pytest.mark.integration
@pytest.mark.db
def test_login_success_returns_session_token(client, middleware_db, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _card: "ACC-1001")
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_args, **_kwargs: _FakeResponse(
            200,
            {
                "accountId": 77,
                "accountNumber": "ACC-1001",
                "token": "jwt-token",
                "balance": 850.0,
                "customerName": "Maryam",
            },
        ),
    )

    resp = client.post("/atm/login", json={"cardNumber": "4111111111111111", "pin": "1234"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["accountNumber"] == "ACC-1001"
    assert isinstance(body["sessionToken"], str) and body["sessionToken"]


@pytest.mark.integration
@pytest.mark.db
def test_withdraw_same_idempotency_key_returns_cached(client, middleware_db, monkeypatch):
    token = "test-session-token-1"
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-1",
            "account_id": 77,
            "account_number": "ACC-1001",
            "card_number": "4111111111111111",
            "balance": 500.0,
            "customer_name": "Maryam",
        },
    )
    monkeypatch.setattr(middleware.sessions, "update_balance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(middleware, "_hash_and_persist", lambda **_kwargs: ("hash-1", "0xabc"))

    calls = {"count": 0}

    def _cb_post_stub(*_args, **_kwargs):
        calls["count"] += 1
        return _FakeResponse(
            200,
            {
                "transactionId": 555,
                "balanceAfter": 400.0,
                "referenceId": "ref-555",
                "createdAt": "2026-05-27T10:00:00Z",
            },
        )

    monkeypatch.setattr(middleware, "_cb_post", _cb_post_stub)

    headers = {"X-Session-Token": token, "Idempotency-Key": "idem-001"}
    first = client.post("/atm/withdraw", json={"amount": 100.0}, headers=headers)
    second = client.post("/atm/withdraw", json={"amount": 100.0}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert calls["count"] == 1


@pytest.mark.integration
@pytest.mark.db
def test_withdraw_same_key_different_body_returns_422(client, middleware_db, monkeypatch):
    token = "test-session-token-2"
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-2",
            "account_id": 88,
            "account_number": "ACC-2002",
            "card_number": "4222222222222222",
            "balance": 600.0,
            "customer_name": "User",
        },
    )
    monkeypatch.setattr(middleware.sessions, "update_balance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(middleware, "_hash_and_persist", lambda **_kwargs: ("hash-2", "0xdef"))
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_args, **_kwargs: _FakeResponse(
            200,
            {
                "transactionId": 777,
                "balanceAfter": 550.0,
                "referenceId": "ref-777",
                "createdAt": "2026-05-27T10:01:00Z",
            },
        ),
    )

    headers = {"X-Session-Token": token, "Idempotency-Key": "idem-dup"}
    first = client.post("/atm/withdraw", json={"amount": 50.0}, headers=headers)
    second = client.post("/atm/withdraw", json={"amount": 40.0}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 422


@pytest.mark.integration
@pytest.mark.db
def test_admin_unlock_requires_service_token(client, middleware_db):
    resp = client.post("/atm/admin/login-unlock", json={"accountNumber": "ACC-3003"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


@pytest.mark.integration
@pytest.mark.db
def test_unknown_client_cert_serial_is_rejected(client, middleware_db, monkeypatch):
    monkeypatch.setattr(middleware.client_cert.config, "CLIENT_CERT_ENFORCE_ALLOWLIST", True)
    monkeypatch.setattr(middleware.client_cert.config, "CLIENT_CERT_ALLOWED_SERIALS", "ABC123")

    resp = client.post(
        "/atm/account-status",
        json={"accountNumber": "ACC-4004"},
        headers={
            "X-Client-Cert-Subject": "CN=rogue-client",
            "X-Client-Cert-Serial": "DEADCAFE",
        },
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Client certificate not on allow-list"


@pytest.mark.integration
@pytest.mark.db
def test_deposit_same_idempotency_key_returns_cached(client, middleware_db, monkeypatch):
    token = "test-session-token-3"
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-3",
            "account_id": 99,
            "account_number": "ACC-9009",
            "card_number": "4333333333333333",
            "balance": 250.0,
            "customer_name": "Demo",
        },
    )
    monkeypatch.setattr(middleware.sessions, "update_balance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(middleware, "_hash_and_persist", lambda **_kwargs: ("hash-3", "0x123"))

    calls = {"count": 0}

    def _cb_post_stub(*_args, **_kwargs):
        calls["count"] += 1
        return _FakeResponse(
            200,
            {
                "transactionId": 888,
                "balanceAfter": 300.0,
                "referenceId": "ref-888",
                "createdAt": "2026-05-27T10:02:00Z",
            },
        )

    monkeypatch.setattr(middleware, "_cb_post", _cb_post_stub)

    headers = {"X-Session-Token": token, "Idempotency-Key": "idem-dep-1"}
    first = client.post("/atm/deposit", json={"amount": 50.0}, headers=headers)
    second = client.post("/atm/deposit", json={"amount": 50.0}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert calls["count"] == 1


@pytest.mark.integration
@pytest.mark.db
def test_deposit_without_idempotency_key_returns_400(client, middleware_db, monkeypatch):
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-4",
            "account_id": 10,
            "account_number": "ACC-1010",
            "card_number": "4444444444444444",
            "balance": 100.0,
            "customer_name": "NoKey",
        },
    )
    resp = client.post("/atm/deposit", json={"amount": 10.0}, headers={"X-Session-Token": "s"})
    assert resp.status_code == 400
    assert "Idempotency-Key header is required" in resp.json()["detail"]


@pytest.mark.integration
@pytest.mark.db
def test_balance_returns_cached_session_balance(client, middleware_db, monkeypatch):
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-5",
            "account_id": 11,
            "account_number": "ACC-1111",
            "card_number": "4555555555555555",
            "balance": 432.1,
            "customer_name": "BalanceUser",
        },
    )
    resp = client.get("/atm/balance", headers={"X-Session-Token": "session-balance"})
    assert resp.status_code == 200
    assert resp.json() == {"balance": 432.1, "accountNumber": "ACC-1111"}


@pytest.mark.integration
@pytest.mark.db
def test_session_continue_invalid_session_returns_401(client, middleware_db, monkeypatch):
    monkeypatch.setattr(middleware.sessions, "touch", lambda _token: False)
    resp = client.post("/atm/session/continue", headers={"X-Session-Token": "expired"})
    assert resp.status_code == 401
    assert "Invalid or expired session" in resp.json()["detail"]


@pytest.mark.integration
@pytest.mark.db
def test_logout_removes_session_and_returns_logged_out(client, middleware_db, monkeypatch):
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-6",
            "account_id": 12,
            "account_number": "ACC-1212",
            "card_number": "4666666666666666",
            "balance": 900.0,
            "customer_name": "LogoutUser",
        },
    )
    removed = {"token": None}

    def _remove(token: str):
        removed["token"] = token

    monkeypatch.setattr(middleware.sessions, "remove", _remove)
    resp = client.post("/atm/logout", headers={"X-Session-Token": "session-logout"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "logged_out"}
    assert removed["token"] == "session-logout"
