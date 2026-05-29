"""Unit tests for retention.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import retention
from models import CorrelationLog, IdempotencyRecord, TransactionLog


@pytest.mark.db
class TestRetentionDisabled:
    def test_purge_transaction_logs_zero_days(self, middleware_db):
        assert retention.purge_transaction_logs(0) == 0

    def test_purge_transaction_logs_negative_days(self, middleware_db):
        assert retention.purge_transaction_logs(-1) == 0

    def test_purge_when_db_disabled(self, db_disabled):
        assert retention.purge_transaction_logs(30) == 0
        assert retention.purge_correlation_logs(30) == 0
        assert retention.purge_expired_idempotency() == 0


@pytest.mark.db
class TestRetentionPurges:
    def _add_old_transaction_log(self):
        import db

        old = datetime.now(timezone.utc) - timedelta(days=100)
        with db.db_session() as s:
            s.add(
                TransactionLog(
                    log_id="old-txn",
                    created_at=old,
                    channel="ATM_WEB",
                    http_method="POST",
                    endpoint="/atm/login",
                    response_status_code=200,
                    outcome="success",
                )
            )

    def test_purge_transaction_logs_deletes_old_rows(self, middleware_db):
        self._add_old_transaction_log()
        deleted = retention.purge_transaction_logs(90)
        assert deleted >= 1

    def test_purge_correlation_logs_deletes_old_rows(self, middleware_db):
        import db

        old = datetime.now(timezone.utc) - timedelta(days=100)
        with db.db_session() as s:
            s.add(
                CorrelationLog(
                    log_id="old-corr",
                    correlation_id="corr-1",
                    step="test",
                    system="middleware",
                    status="ok",
                    created_at=old,
                )
            )
        assert retention.purge_correlation_logs(90) >= 1

    def test_purge_expired_idempotency(self, middleware_db):
        import db

        now = datetime.now(timezone.utc)
        with db.db_session() as s:
            s.add(
                IdempotencyRecord(
                    account_number="ACC",
                    idempotency_key="exp-key",
                    endpoint="/atm/deposit",
                    request_fingerprint="fp",
                    status="completed",
                    created_at=now - timedelta(days=2),
                    expires_at=now - timedelta(hours=1),
                )
            )
        assert retention.purge_expired_idempotency() >= 1

    def test_run_retention_returns_all_counts(self, middleware_db):
        result = retention.run_retention(90)
        assert "transaction_logs" in result
        assert "correlation_logs" in result
        assert "idempotency_records" in result

    def test_purge_permanently_locked_without_core_banking(self, middleware_db):
        import db
        from datetime import timedelta

        from models import LoginLockout

        old = datetime.now(timezone.utc) - timedelta(days=90)
        with db.db_session() as s:
            s.add(
                LoginLockout(
                    account_number="STALE-LOCK",
                    failed_attempts=9,
                    locked_until=None,
                    lock_tier=2,
                    permanently_locked=True,
                    must_reset_pin=False,
                    updated_at=old,
                )
            )
        closed = retention.purge_permanently_locked_accounts(cleanup_days=60)
        assert closed == 1

    def test_purge_permanently_locked_zero_days(self, middleware_db):
        assert retention.purge_permanently_locked_accounts(cleanup_days=0) == 0

    def test_close_account_via_core_banking_success(self, monkeypatch):
        customers = [{"customerId": 1}]
        accounts = [{"accountNumber": "ACC-CLOSE", "accountId": 99}]

        class _Resp:
            def __init__(self, ok, payload=None, status_code=200, text=""):
                self.ok = ok
                self._payload = payload or []
                self.status_code = status_code
                self.text = text

            def json(self):
                return self._payload

        def fake_get(url, **kwargs):
            if url.endswith("/customers"):
                return _Resp(True, customers)
            if "/accounts" in url:
                return _Resp(True, accounts)
            return _Resp(False, status_code=404)

        def fake_delete(url, **kwargs):
            return _Resp(True, status_code=204)

        monkeypatch.setattr(retention.cb_http, "get", fake_get)
        monkeypatch.setattr(retention.cb_http, "delete", fake_delete)
        assert retention._close_account_via_core_banking(
            "ACC-CLOSE", "https://bank.local", "jwt"
        )

    def test_close_account_not_found(self, monkeypatch):
        monkeypatch.setattr(
            retention.cb_http,
            "get",
            lambda url, **kwargs: type("R", (), {"ok": True, "json": lambda: []})(),
        )
        assert not retention._close_account_via_core_banking(
            "MISSING", "https://bank.local", None
        )

    def test_close_account_get_customers_fails(self, monkeypatch):
        monkeypatch.setattr(
            retention.cb_http,
            "get",
            lambda url, **kwargs: type("R", (), {"ok": False, "status_code": 500})(),
        )
        assert not retention._close_account_via_core_banking(
            "ACC", "https://bank.local", None
        )

    def test_recent_logs_not_purged(self, middleware_db):
        import db

        now = datetime.now(timezone.utc)
        with db.db_session() as s:
            s.add(
                TransactionLog(
                    log_id="new-txn",
                    created_at=now,
                    channel="ATM_WEB",
                    http_method="GET",
                    endpoint="/health",
                    response_status_code=200,
                    outcome="success",
                )
            )
        before = retention.purge_transaction_logs(90)
        import db as db_mod

        with db_mod.db_session() as s:
            row = s.get(TransactionLog, "new-txn")
            assert row is not None
        assert before >= 0
