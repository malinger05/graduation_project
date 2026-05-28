"""Focused unit tests for middleware.py helper logic."""

from __future__ import annotations

import pytest
from fastapi import HTTPException


class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json_data


def test_resolve_channel_default_and_trim():
    import middleware
    import transaction_logs

    assert middleware._resolve_channel(None) == transaction_logs.DEFAULT_CHANNEL
    assert middleware._resolve_channel("   ") == transaction_logs.DEFAULT_CHANNEL
    assert middleware._resolve_channel("  ATM  ") == "ATM"


def test_require_idempotency_key():
    import middleware

    assert middleware._require_idempotency_key("  abc  ") == "abc"
    with pytest.raises(HTTPException, match="Idempotency-Key header is required"):
        middleware._require_idempotency_key("  ")


def test_require_service_token(monkeypatch):
    import middleware

    monkeypatch.setattr(middleware, "SERVICE_TOKEN", "svc-token")
    middleware._require_service_token("svc-token")
    with pytest.raises(HTTPException, match="Unauthorized"):
        middleware._require_service_token("wrong")


def test_cb_post_sets_headers_and_auth(monkeypatch):
    import middleware

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(middleware.cb_http, "post", fake_post)
    monkeypatch.setattr(middleware, "CORE_BANKING_URL", "https://bank.local")

    resp = middleware._cb_post("/x", {"a": 1}, token="jwt", extra_headers={"X-A": "1"})
    assert resp.status_code == 200
    assert captured["url"] == "https://bank.local/x"
    assert captured["kwargs"]["json"] == {"a": 1}
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer jwt"
    assert captured["kwargs"]["headers"]["X-A"] == "1"


def test_cb_post_connection_error(monkeypatch):
    import middleware
    import requests

    def raise_conn(*args, **kwargs):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(middleware.cb_http, "post", raise_conn)
    monkeypatch.setattr(middleware, "CORE_BANKING_URL", "https://bank.local")

    with pytest.raises(HTTPException, match="Cannot reach Core Banking"):
        middleware._cb_post("/x", {"a": 1})


def test_cb_post_service_requires_token(monkeypatch):
    import middleware

    monkeypatch.setattr(middleware, "SERVICE_TOKEN", "")
    with pytest.raises(HTTPException, match="MIDDLEWARE_SERVICE_TOKEN not configured"):
        middleware._cb_post_service("/p", {"k": "v"})


def test_cb_post_service_passes_service_header(monkeypatch):
    import middleware

    captured = {}

    def fake_cb_post(path, body, token=None, extra_headers=None):
        captured["path"] = path
        captured["body"] = body
        captured["token"] = token
        captured["extra_headers"] = extra_headers
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(middleware, "SERVICE_TOKEN", "svc")
    monkeypatch.setattr(middleware, "_cb_post", fake_cb_post)

    middleware._cb_post_service("/p", {"k": "v"})
    assert captured["extra_headers"] == {"X-Service-Token": "svc"}


def test_resolve_card_to_account_paths(monkeypatch):
    import middleware

    monkeypatch.setattr(middleware, "CORE_BANKING_URL", "https://bank.local")
    monkeypatch.setattr(
        middleware.cb_http,
        "get",
        lambda *args, **kwargs: _Resp(200, {"accountNumber": "ACC1"}),
    )
    assert middleware._resolve_card_to_account("4111") == "ACC1"

    monkeypatch.setattr(middleware.cb_http, "get", lambda *args, **kwargs: _Resp(404, {}))
    assert middleware._resolve_card_to_account("4111") is None


def test_resolve_card_to_account_connection_error(monkeypatch):
    import middleware
    import requests

    def raise_conn(*args, **kwargs):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(middleware.cb_http, "get", raise_conn)
    with pytest.raises(HTTPException, match="Cannot reach Core Banking"):
        middleware._resolve_card_to_account("4111")


def test_health_returns_payload():
    import middleware

    body = middleware.health()
    assert body["status"] == "ok"
    assert body["service"] == "ATM Middleware"


def test_hash_and_persist_no_admin(monkeypatch):
    import middleware

    monkeypatch.setattr(middleware, "hash_transaction", lambda **kwargs: "HASH123")
    monkeypatch.setattr(middleware, "_submit_to_blockchain", lambda hash_str: "0xtx")
    monkeypatch.setattr(middleware, "_get_admin_client", lambda: None)

    out_hash, out_tx = middleware._hash_and_persist(
        transaction_id=1,
        account_number="ACC1",
        transaction_type="DEPOSIT",
        amount=5.0,
        balance_after=10.0,
        reference_id="R1",
        created_at="2026-01-01T00:00:00Z",
    )
    assert out_hash == "HASH123"
    assert out_tx == "0xtx"


def test_hash_and_persist_handles_submit_exception(monkeypatch):
    import middleware

    class _Admin:
        def __init__(self):
            self.called = False

        def patch_blockchain(self, **kwargs):
            self.called = True

    admin = _Admin()
    monkeypatch.setattr(middleware, "hash_transaction", lambda **kwargs: "HASHERR")

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(middleware, "_submit_to_blockchain", _raise)
    monkeypatch.setattr(middleware, "_get_admin_client", lambda: admin)

    out_hash, out_tx = middleware._hash_and_persist(
        transaction_id=2,
        account_number="ACC2",
        transaction_type="WITHDRAW",
        amount=6.0,
        balance_after=8.0,
        reference_id="R2",
        created_at="2026-01-01T00:00:00Z",
    )
    assert out_hash == "HASHERR"
    assert out_tx is None
    assert admin.called is True


def test_atm_session_continue_success_and_failure(monkeypatch):
    import middleware

    monkeypatch.setattr(middleware.sessions, "touch", lambda token: True)
    monkeypatch.setattr(middleware, "_audit", lambda **kwargs: None)
    assert middleware.atm_session_continue("tok", x_channel=None) == {"status": "ok"}

    monkeypatch.setattr(middleware.sessions, "touch", lambda token: False)
    with pytest.raises(HTTPException, match="Invalid or expired session"):
        middleware.atm_session_continue("tok", x_channel=None)


def test_get_transactions_non_ok_and_connection_error(monkeypatch):
    import middleware
    import requests

    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda token: {"account_id": 1, "jwt": "j", "account_number": "A1"},
    )
    monkeypatch.setattr(
        middleware.cb_http,
        "get",
        lambda *args, **kwargs: _Resp(500, {}, "bad"),
    )
    with pytest.raises(HTTPException, match="bad"):
        middleware.get_transactions("tok", x_channel=None)

    def raise_conn(*args, **kwargs):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(middleware.cb_http, "get", raise_conn)
    with pytest.raises(HTTPException, match="Cannot reach Core Banking"):
        middleware.get_transactions("tok", x_channel=None)


def test_get_transactions_success(monkeypatch):
    import middleware

    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda token: {"account_id": 1, "jwt": "j", "account_number": "A1"},
    )
    monkeypatch.setattr(
        middleware.cb_http,
        "get",
        lambda *args, **kwargs: _Resp(200, [{"id": 1}], ""),
    )
    monkeypatch.setattr(middleware, "_audit", lambda **kwargs: None)

    body = middleware.get_transactions("tok", x_channel=None)
    assert body == [{"id": 1}]


def test_atm_ack_success(monkeypatch):
    import middleware

    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda token: {"account_id": 7, "jwt": "jwt", "account_number": "ACC7"},
    )
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda path, body, token=None, extra_headers=None: _Resp(200, {"dispenseStatus": "DISPENSED"}),
    )
    monkeypatch.setattr(middleware, "_audit", lambda **kwargs: None)
    monkeypatch.setattr(middleware.correlation, "new_correlation_id", lambda: "corr")
    monkeypatch.setattr(middleware.correlation, "log_step", lambda *args, **kwargs: None)

    req = middleware.AckRequest(middlewareTxId=123)
    body = middleware.atm_ack(req, "tok", x_channel=None)
    assert body["status"] == "CONFIRMED"
    assert body["transactionId"] == 123
    assert body["dispenseStatus"] == "DISPENSED"


def test_atm_ack_non_ok_raises(monkeypatch):
    import middleware

    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda token: {"account_id": 7, "jwt": "jwt", "account_number": "ACC7"},
    )
    monkeypatch.setattr(
        middleware,
        "_cb_post",
        lambda path, body, token=None, extra_headers=None: _Resp(502, {}, "bad gateway"),
    )
    monkeypatch.setattr(middleware.correlation, "new_correlation_id", lambda: "corr")
    monkeypatch.setattr(middleware.correlation, "log_step", lambda *args, **kwargs: None)

    req = middleware.AckRequest(middlewareTxId=123)
    with pytest.raises(HTTPException, match="bad gateway"):
        middleware.atm_ack(req, "tok", x_channel=None)


def test_atm_tx_status_success_and_non_ok(monkeypatch):
    import middleware

    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda token: {"account_id": 1, "jwt": "j", "account_number": "A1"},
    )
    monkeypatch.setattr(middleware, "_audit", lambda **kwargs: None)

    monkeypatch.setattr(
        middleware.cb_http,
        "get",
        lambda *args, **kwargs: _Resp(200, {"id": 9, "status": "DONE"}),
    )
    ok_body = middleware.atm_tx_status(9, "tok", x_channel=None)
    assert ok_body["id"] == 9

    monkeypatch.setattr(
        middleware.cb_http,
        "get",
        lambda *args, **kwargs: _Resp(404, {}, "not found"),
    )
    with pytest.raises(HTTPException, match="not found"):
        middleware.atm_tx_status(9, "tok", x_channel=None)


def test_atm_tx_status_connection_error(monkeypatch):
    import middleware
    import requests

    monkeypatch.setattr(
        middleware.sessions,
        "get",
        lambda token: {"account_id": 1, "jwt": "j", "account_number": "A1"},
    )

    def raise_conn(*args, **kwargs):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(middleware.cb_http, "get", raise_conn)
    with pytest.raises(HTTPException, match="Cannot reach Core Banking"):
        middleware.atm_tx_status(9, "tok", x_channel=None)
