"""Unit tests for lockouts.py — Postgres/SQLite persistence path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import db
from models import LoginLockout


def _fail_n(lockouts, account: str, n: int):
    for _ in range(n):
        lockouts.record_failure(account)


@pytest.mark.db
class TestLockoutsDbCheck:
    def test_check_unknown_account_returns_none(self, middleware_db, lockouts_standard):
        assert lockouts_standard.check("db-unknown") is None

    def test_timed_lock_expires_on_check(self, middleware_db, lockouts_fast, utc_now):
        account = "db-expire-check"
        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_fast, account, 3)
            assert lockouts_fast.check(account)["status"] == "locked"
        later = utc_now + timedelta(minutes=1)
        with patch("lockouts._now", return_value=later):
            assert lockouts_fast.check(account) is None

    def test_pin_reset_required_from_db(self, middleware_db, lockouts_standard):
        lockouts_standard.admin_unlock("db-pin-reset")
        assert lockouts_standard.check("db-pin-reset") == {"status": "pin_reset_required"}

    def test_permanent_lock_from_db(self, middleware_db, lockouts_standard, utc_now):
        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_standard, "db-perm", 9)
        c = lockouts_standard.check("db-perm")
        assert c is not None
        assert c.get("admin_unlock_required") is True


@pytest.mark.db
class TestLockoutsDbMutations:
    def test_reset_deletes_non_permanent_row(self, middleware_db, lockouts_standard):
        _fail_n(lockouts_standard, "db-reset", 2)
        lockouts_standard.reset("db-reset")
        assert lockouts_standard.check("db-reset") is None

    def test_requires_pin_reset_and_complete(self, middleware_db, lockouts_standard):
        lockouts_standard.admin_unlock("db-complete")
        assert lockouts_standard.requires_pin_reset("db-complete") is True
        lockouts_standard.complete_pin_reset("db-complete")
        assert lockouts_standard.requires_pin_reset("db-complete") is False

    def test_cleanup_expired_clears_db_timed_lock(self, middleware_db, lockouts_fast, utc_now):
        import lockouts as lo

        account = "db-cleanup"
        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_fast, account, 3)
        later = utc_now + timedelta(minutes=1)
        with patch("lockouts._now", return_value=later):
            n = lo.cleanup_expired()
        assert n >= 1
        assert lockouts_fast.check(account) is None

    def test_naive_datetime_locked_until_handled(self, middleware_db, lockouts_standard):
        """Cover _as_utc branch for timezone-naive DB timestamps."""
        import lockouts as lo

        account = "db-naive-ts"
        naive_until = datetime.utcnow() + timedelta(minutes=30)
        with db.db_session() as s:
            s.add(
                LoginLockout(
                    account_number=account,
                    failed_attempts=3,
                    locked_until=naive_until,
                    lock_tier=1,
                    permanently_locked=False,
                    must_reset_pin=False,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        result = lo.check(account)
        assert result is not None
        assert result["status"] == "locked"
