"""Unit tests for transaction_logs.py — sanitize and log_event."""

from __future__ import annotations

import pytest

import db
import transaction_logs
from models import TransactionLog


class TestSanitize:
    def test_redacts_pin_key(self):
        out = transaction_logs.sanitize({"pin": "9999", "amount": 10})
        assert out["pin"] == "***REDACTED***"
        assert out["amount"] == 10

    def test_redacts_case_insensitive(self):
        out = transaction_logs.sanitize({"PIN": "1111"})
        assert out["PIN"] == "***REDACTED***"

    def test_redacts_jwt_and_tokens(self):
        data = {
            "jwt": "secret",
            "token": "t",
            "x-session-token": "s",
            "authorization": "Bearer x",
        }
        out = transaction_logs.sanitize(data)
        for k in data:
            assert out[k] == "***REDACTED***"

    def test_nested_dict_sanitized(self):
        out = transaction_logs.sanitize({"user": {"password": "p", "name": "Ann"}})
        assert out["user"]["password"] == "***REDACTED***"
        assert out["user"]["name"] == "Ann"

    def test_list_sanitized(self):
        out = transaction_logs.sanitize([{"pin": "1"}, {"ok": True}])
        assert out[0]["pin"] == "***REDACTED***"
        assert out[1]["ok"] is True

    def test_scalar_unchanged(self):
        assert transaction_logs.sanitize("hello") == "hello"
        assert transaction_logs.sanitize(42) == 42

    def test_eth_private_key_redacted(self):
        out = transaction_logs.sanitize({"eth_private_key": "0xabc"})
        assert out["eth_private_key"] == "***REDACTED***"


@pytest.mark.db
class TestLogEvent:
    def test_log_event_persists_sanitized_bodies(self, middleware_db):
        transaction_logs.log_event(
            endpoint="/atm/login",
            http_method="POST",
            outcome="success",
            response_status_code=200,
            account_number="ACC1",
            request_body={"pin": "1234"},
            response_body={"token": "sess"},
        )
        with db.db_session() as s:
            row = s.query(TransactionLog).one()
        assert row.request_body["pin"] == "***REDACTED***"
        assert row.response_body["token"] == "***REDACTED***"
        assert row.outcome == "success"

    def test_log_event_db_disabled_noop(self, db_disabled):
        transaction_logs.log_event(
            endpoint="/health",
            http_method="GET",
            outcome="success",
            response_status_code=200,
        )

    def test_log_event_cached_outcome(self, middleware_db):
        transaction_logs.log_event(
            endpoint="/atm/deposit",
            http_method="POST",
            outcome="cached",
            response_status_code=200,
            idempotency_key="idem-1",
        )
        with db.db_session() as s:
            row = s.query(TransactionLog).one()
        assert row.outcome == "cached"
        assert row.idempotency_key == "idem-1"

    def test_log_event_with_correlation_and_duration(self, middleware_db):
        transaction_logs.log_event(
            endpoint="/atm/withdraw",
            http_method="POST",
            outcome="error",
            response_status_code=400,
            correlation_id="corr-xyz",
            duration_ms=150,
            error_message="Insufficient funds",
        )
        with db.db_session() as s:
            row = s.query(TransactionLog).one()
        assert row.correlation_id == "corr-xyz"
        assert row.duration_ms == 150
        assert row.error_message == "Insufficient funds"
