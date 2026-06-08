"""Resilience / failure-path integration tests for middleware API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

import middleware
from models import IdempotencyRecord


@dataclass
class _FakeResponse:
    status_code: int
    _payload: dict[str, Any]
    text: str = ""

    def __post_init__(self) -> None:
        if not self.text:
            self.text = f"error-{self.status_code}"

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


def _session_stub(monkeypatch, *, account_number: str = "ACC-9001", account_id: int = 77):
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-resilience",
            "account_id": account_id,
            "account_number": account_number,
            "card_number": "4111111111111111",
            "balance": 500.0,
            "customer_name": "ResilienceUser",
        },
    )
    monkeypatch.setattr(middleware.sessions, "update_balance", lambda *_args, **_kwargs: None)


def _patch_cb_post_connection_error(monkeypatch) -> None:
    """Simulate Core Banking unreachable through real _cb_post error handling."""

    def _raise_conn(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError("core banking down")

    monkeypatch.setattr(middleware.cb_http, "post", _raise_conn)


@pytest.mark.integration
@pytest.mark.db
def test_login_cbs_unreachable_returns_503(client, middleware_db, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _card: "ACC-LOGIN")
    _patch_cb_post_connection_error(monkeypatch)

    resp = client.post("/atm/login", json={"cardNumber": "4111111111111111", "pin": "1234"})

    assert resp.status_code == 503
    assert "Cannot reach Core Banking" in resp.json()["detail"]


@pytest.mark.integration
@pytest.mark.db
def test_withdraw_cbs_unreachable_returns_503(client, middleware_db, monkeypatch):
    _session_stub(monkeypatch)
    _patch_cb_post_connection_error(monkeypatch)

    resp = client.post(
        "/atm/withdraw",
        json={"amount": 25.0},
        headers={"X-Session-Token": "tok", "Idempotency-Key": "res-withdraw-503"},
    )

    assert resp.status_code == 503
    assert "Cannot reach Core Banking" in resp.json()["detail"]


@pytest.mark.integration
@pytest.mark.db
def test_deposit_cbs_error_status_is_passthrough(client, middleware_db, monkeypatch):
    _session_stub(monkeypatch)
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_args, **_kwargs: _FakeResponse(500, {}, text="internal server error"),
    )

    resp = client.post(
        "/atm/deposit",
        json={"amount": 10.0},
        headers={"X-Session-Token": "tok", "Idempotency-Key": "res-deposit-500"},
    )

    assert resp.status_code == 500
    assert "internal server error" in resp.json()["detail"]


@pytest.mark.integration
@pytest.mark.db
def test_deposit_blockchain_failure_still_succeeds(client, middleware_db, monkeypatch):
    _session_stub(monkeypatch)
    monkeypatch.setattr(middleware, "_hash_and_persist", lambda **_kwargs: ("hash-no-chain", None))
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_args, **_kwargs: _FakeResponse(
            200,
            {
                "transactionId": 901,
                "balanceAfter": 525.0,
                "referenceId": "ref-901",
                "createdAt": "2026-05-29T12:00:00Z",
            },
        ),
    )

    resp = client.post(
        "/atm/deposit",
        json={"amount": 25.0},
        headers={"X-Session-Token": "tok", "Idempotency-Key": "res-deposit-bc-fail"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["canonicalHash"] == "hash-no-chain"
    assert body["blockchainTx"] is None
    assert "retry" in body["message"].lower()


@pytest.mark.integration
@pytest.mark.db
def test_withdraw_cbs_failure_then_retry_same_key_returns_409(
    client, middleware_db, monkeypatch
):
    account_number = "ACC-RETRY"
    _session_stub(monkeypatch, account_number=account_number)
    _patch_cb_post_connection_error(monkeypatch)

    headers = {"X-Session-Token": "tok", "Idempotency-Key": "res-withdraw-inflight"}
    first = client.post("/atm/withdraw", json={"amount": 15.0}, headers=headers)
    assert first.status_code == 503

    with middleware_db() as s:
        rec = s.get(IdempotencyRecord, (account_number, "res-withdraw-inflight"))
        assert rec is not None
        assert rec.status == "in_progress"

    second = client.post("/atm/withdraw", json={"amount": 15.0}, headers=headers)
    assert second.status_code == 409
    assert "still being processed" in second.json()["detail"].lower()


@pytest.mark.integration
@pytest.mark.db
def test_withdraw_cbs_failure_does_not_update_session_balance(
    client, middleware_db, monkeypatch
):
    updated = {"called": False}

    def _update_balance(*_args, **_kwargs):
        updated["called"] = True

    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _token: {
            "jwt": "jwt-resilience",
            "account_id": 77,
            "account_number": "ACC-BAL",
            "card_number": "4111111111111111",
            "balance": 500.0,
            "customer_name": "ResilienceUser",
        },
    )
    monkeypatch.setattr(middleware.sessions, "update_balance", _update_balance)
    _patch_cb_post_connection_error(monkeypatch)

    resp = client.post(
        "/atm/withdraw",
        json={"amount": 20.0},
        headers={"X-Session-Token": "tok", "Idempotency-Key": "res-withdraw-balance"},
    )

    assert resp.status_code == 503
    assert updated["called"] is False
