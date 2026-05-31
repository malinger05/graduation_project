"""In-process FastAPI tests for middleware.py route handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import middleware


@dataclass
class _FakeResponse:
    status_code: int
    _payload: dict[str, Any] | list[Any] | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            return {}
        return self._payload


@pytest.fixture
def api_client(middleware_db, monkeypatch):
    monkeypatch.setattr(middleware.db, "init_db", lambda: True)
    monkeypatch.setattr(middleware.blockchain_worker, "start", lambda *args, **kwargs: None)
    with TestClient(middleware.app) as client:
        yield client


@pytest.mark.db
def test_login_success_persists_session(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: "ACC-OK")
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_a, **_k: _FakeResponse(
            200,
            {
                "accountId": 1,
                "accountNumber": "ACC-OK",
                "token": "jwt",
                "balance": 100.0,
                "customerName": "Test",
            },
        ),
    )
    resp = api_client.post("/atm/login", json={"cardNumber": "4111", "pin": "1234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sessionToken"]


@pytest.mark.db
def test_login_wrong_pin_records_failure(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: "ACC-FAIL")
    monkeypatch.setattr(middleware, "_cb_post", lambda *_a, **_k: _FakeResponse(401, {}))
    resp = api_client.post("/atm/login", json={"cardNumber": "4111", "pin": "0000"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "invalid"
    assert resp.json()["attempts_to_next_lock"] == 2


@pytest.mark.db
def test_login_unknown_card_still_calls_cb(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: None)
    monkeypatch.setattr(middleware, "_cb_post", lambda *_a, **_k: _FakeResponse(401, {}))
    resp = api_client.post("/atm/login", json={"cardNumber": "4999", "pin": "0000"})
    assert resp.status_code == 200
    assert resp.json()["attempts_to_next_lock"] == 3


@pytest.mark.db
def test_login_core_banking_403_returns_locked_message(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: "ACC-403")
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_a, **_k: _FakeResponse(403, {"error": "Card cancelled"}),
    )
    resp = api_client.post("/atm/login", json={"cardNumber": "4111", "pin": "1234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "locked"
    assert "cancelled" in body["message"].lower()


@pytest.mark.db
def test_login_core_banking_502(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: "ACC-502")
    monkeypatch.setattr(middleware, "_cb_post", lambda *_a, **_k: _FakeResponse(502, {}, "down"))
    resp = api_client.post("/atm/login", json={"cardNumber": "4111", "pin": "1234"})
    assert resp.status_code == 502


@pytest.mark.db
def test_account_status_by_account_number_with_lockout(api_client, monkeypatch):
    import lockouts

    lockouts.admin_unlock("ACC-STATUS")
    resp = api_client.post("/atm/account-status", json={"accountNumber": "ACC-STATUS"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pin_reset_required"


@pytest.mark.db
def test_account_status_unknown_card_returns_ok(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: None)
    resp = api_client.post("/atm/account-status", json={"cardNumber": "4999"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "accountNumber": None}


@pytest.mark.db
def test_account_status_requires_identifier(api_client):
    resp = api_client.post("/atm/account-status", json={})
    assert resp.status_code == 400


@pytest.mark.db
def test_admin_unlock_with_service_token(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "SERVICE_TOKEN", "svc-test")
    resp = api_client.post(
        "/atm/admin/login-unlock",
        json={"accountNumber": "ACC-UNLOCK"},
        headers={"X-Service-Token": "svc-test"},
    )
    assert resp.status_code == 200
    assert resp.json()["mustResetPin"] is True


@pytest.mark.db
def test_reset_pin_validation_and_success(api_client, monkeypatch):
    import lockouts

    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: "ACC-PIN")
    lockouts.admin_unlock("ACC-PIN")

    mismatch = api_client.post(
        "/atm/reset-pin",
        json={"cardNumber": "4111", "newPin": "1234", "confirmPin": "5678"},
    )
    assert mismatch.status_code == 200
    assert mismatch.json()["status"] == "error"

    bad_fmt = api_client.post(
        "/atm/reset-pin",
        json={"cardNumber": "4111", "newPin": "12", "confirmPin": "12"},
    )
    assert bad_fmt.json()["message"] == "PIN must be exactly 4 digits."

    monkeypatch.setattr(
        middleware,
        "_cb_post_service",
        lambda *_a, **_k: _FakeResponse(200, {"status": "ok"}),
    )
    ok = api_client.post(
        "/atm/reset-pin",
        json={"cardNumber": "4111", "newPin": "5678", "confirmPin": "5678"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "ok"
    assert not lockouts.requires_pin_reset("ACC-PIN")


@pytest.mark.db
def test_reset_pin_card_not_found(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "_resolve_card_to_account", lambda _c: None)
    resp = api_client.post(
        "/atm/reset-pin",
        json={"cardNumber": "4111", "newPin": "1234", "confirmPin": "1234"},
    )
    assert resp.status_code == 404


@pytest.mark.db
def test_create_card_returns_409(api_client):
    resp = api_client.post("/atm/create-card", json={"accountNumber": "ACC-X"})
    assert resp.status_code == 409


@pytest.mark.db
def test_set_own_pin_validation(api_client):
    resp = api_client.post(
        "/atm/set-own-pin",
        json={
            "cardId": 1,
            "accountNumber": "ACC1",
            "pin": "1234",
            "pinConfirm": "5678",
        },
    )
    assert resp.status_code == 400

    resp2 = api_client.post(
        "/atm/set-own-pin",
        json={
            "cardId": 1,
            "accountNumber": "ACC1",
            "pin": "12",
            "pinConfirm": "12",
        },
    )
    assert resp2.status_code == 400


@pytest.mark.db
def test_prepare_own_pin_requires_account(api_client):
    resp = api_client.post("/atm/prepare-own-pin", json={"accountNumber": "  "})
    assert resp.status_code == 400


@pytest.mark.db
def test_card_setup_status_stub(api_client):
    resp = api_client.get("/atm/card-setup-status/ACC1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.db
def test_deposit_happy_path(api_client, monkeypatch):
    token = "dep-session"
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _t: {
            "jwt": "jwt",
            "account_id": 5,
            "account_number": "ACC-DEP",
            "card_number": "4111",
            "balance": 100.0,
            "customer_name": "Dep",
        },
    )
    monkeypatch.setattr(middleware.sessions, "update_balance", lambda *_a, **_k: None)
    monkeypatch.setattr(middleware, "_hash_and_persist", lambda **_k: ("hash-dep", "0xdep"))
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_a, **_k: _FakeResponse(
            200,
            {
                "transactionId": 50,
                "balanceAfter": 150.0,
                "referenceId": "ref-dep",
                "createdAt": "2026-05-28T12:00:00Z",
            },
        ),
    )
    resp = api_client.post(
        "/atm/deposit",
        json={"amount": 50.0},
        headers={"X-Session-Token": token, "Idempotency-Key": "idem-dep"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["newBalance"] == 150.0


@pytest.mark.db
def test_withdraw_happy_path(api_client, monkeypatch):
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _t: {
            "jwt": "jwt",
            "account_id": 8,
            "account_number": "ACC-WOK",
            "card_number": "4333",
            "balance": 200.0,
            "customer_name": "WdOk",
        },
    )
    monkeypatch.setattr(middleware.sessions, "update_balance", lambda *_a, **_k: None)
    monkeypatch.setattr(middleware, "_hash_and_persist", lambda **_k: ("hash-wd", "0xwd"))
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_a, **_k: _FakeResponse(
            200,
            {
                "transactionId": 60,
                "balanceAfter": 150.0,
                "referenceId": "ref-wd",
                "createdAt": "2026-05-28T12:00:00Z",
            },
        ),
    )
    resp = api_client.post(
        "/atm/withdraw",
        json={"amount": 50.0},
        headers={"X-Session-Token": "tok-wd", "Idempotency-Key": "idem-wd-ok"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"


@pytest.mark.db
def test_withdraw_insufficient_funds(api_client, monkeypatch):
    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda _t: {
            "jwt": "jwt",
            "account_id": 6,
            "account_number": "ACC-WD",
            "card_number": "4222",
            "balance": 50.0,
            "customer_name": "Wd",
        },
    )
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda *_a, **_k: _FakeResponse(400, {}, "insufficient"),
    )
    resp = api_client.post(
        "/atm/withdraw",
        json={"amount": 100.0},
        headers={"X-Session-Token": "tok", "Idempotency-Key": "idem-wd"},
    )
    assert resp.status_code == 400
    assert "Insufficient" in resp.json()["detail"]


@pytest.mark.db
def test_set_own_pin_forwards_to_core_banking(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "SERVICE_TOKEN", "svc")
    monkeypatch.setattr(
        middleware.cb_http,
        "post",
        lambda url, **kwargs: _FakeResponse(200, {"status": "ok"}),
    )
    resp = api_client.post(
        "/atm/set-own-pin",
        json={
            "cardId": 9,
            "accountNumber": "acc9",
            "pin": "1234",
            "pinConfirm": "1234",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.db
def test_prepare_own_pin_forwards_to_core_banking(api_client, monkeypatch):
    monkeypatch.setattr(middleware, "SERVICE_TOKEN", "svc")
    monkeypatch.setattr(
        middleware.cb_http,
        "post",
        lambda url, **kwargs: _FakeResponse(200, {"cardId": 9}),
    )
    resp = api_client.post("/atm/prepare-own-pin", json={"accountNumber": "ACC9"})
    assert resp.status_code == 200
    assert resp.json()["cardId"] == 9


def test_health_cert_headers(api_client):
    resp = api_client.get(
        "/health/cert-headers",
        headers={
            "X-Client-Cert-Subject": "CN=test",
            "X-Client-Cert-Serial": "AB12",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_cert_subject"] == "CN=test"
    assert body["client_cert_serial"] == "AB12"


@pytest.mark.db
def test_request_body_too_large_returns_413(api_client):
    resp = api_client.post(
        "/atm/login",
        json={"cardNumber": "x", "pin": "y"},
        headers={"Content-Length": "2097152"},
    )
    assert resp.status_code == 413


@pytest.mark.db
def test_http_exception_handler_audits_atm_path(api_client, monkeypatch):
    from fastapi import HTTPException

    def _raise_401(_token: str):
        raise HTTPException(401, "bad session")

    monkeypatch.setattr(middleware.sessions, "get", _raise_401)
    resp = api_client.get("/atm/balance", headers={"X-Session-Token": "tok"})
    assert resp.status_code == 401


@pytest.mark.db
def test_get_admin_client_cached(monkeypatch):
    middleware._admin_client = None
    monkeypatch.setattr(middleware, "SERVICE_TOKEN", "svc")
    first = middleware._get_admin_client()
    second = middleware._get_admin_client()
    assert first is second
    middleware._admin_client = None


@pytest.mark.db
def test_hash_and_persist_with_correlation_and_admin_patch_error(api_client, monkeypatch):
    corr = "corr-xyz"

    class _Admin:
        def patch_blockchain(self, **kwargs):
            raise RuntimeError("patch failed")

    monkeypatch.setattr(middleware, "hash_transaction", lambda **kwargs: "HASH")
    monkeypatch.setattr(middleware, "_get_admin_client", lambda: _Admin())

    h, tx = middleware._hash_and_persist(
        transaction_id=9,
        account_number="A",
        transaction_type="DEPOSIT",
        amount=1.0,
        balance_after=2.0,
        reference_id="R",
        created_at="2026-01-01T00:00:00Z",
        correlation_id=corr,
    )
    assert h == "HASH"
    assert tx is None


@pytest.mark.db
def test_init_blockchain_and_submit_mocked(monkeypatch):
    fake_bc = {
        "w3": MagicMock(),
        "account": MagicMock(address="0xabc"),
        "contract": MagicMock(),
    }
    fake_bc["w3"].eth.get_block.return_value = {"baseFeePerGas": 100}
    fake_bc["w3"].eth.gas_price = 100
    fake_bc["w3"].to_wei.return_value = 2
    fake_bc["w3"].eth.get_transaction_count.return_value = 0
    fake_bc["w3"].eth.chain_id = 11155111
    signed = MagicMock()
    signed.raw_transaction = b"\x01"
    fake_bc["account"].sign_transaction.return_value = signed
    fake_bc["w3"].eth.send_raw_transaction.return_value.hex.return_value = "0xdead"
    fake_bc["contract"].functions.storeLog.return_value.build_transaction.return_value = {}

    monkeypatch.setattr(middleware, "_blockchain", None)
    monkeypatch.setattr(middleware, "_init_blockchain", lambda: fake_bc)
    monkeypatch.setattr(middleware, "CONTRACT_ADDRESS", "0x1")
    monkeypatch.setattr(middleware, "ETH_PRIVATE_KEY", "0xkey")

    tx = middleware._submit_to_blockchain("hash")
    assert tx == "0xdead"
    receipt = middleware._get_chain_receipt("0xdead")
    assert receipt == {"status": 1}
    assert middleware._verify_log_on_chain("hash") is True
