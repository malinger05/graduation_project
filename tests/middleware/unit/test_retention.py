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
