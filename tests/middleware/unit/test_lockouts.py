"""Unit tests for lockouts.py — progressive login lockout (in-memory mode)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def _fail_n(lockouts, account: str, n: int):
    results = []
    for _ in range(n):
        results.append(lockouts.record_failure(account))
    return results


@pytest.mark.memory
class TestLockoutCheck:
    def test_check_unknown_account_returns_none(self, lockouts_standard):
        assert lockouts_standard.check("unknown") is None

    def test_check_after_single_failure_not_locked(self, lockouts_standard):
        lockouts_standard.record_failure("ACC1")
        assert lockouts_standard.check("ACC1") is None


@pytest.mark.memory
class TestLockoutProgressiveFailures:
    def test_first_failure_invalid_with_two_remaining(self, lockouts_standard):
        r = lockouts_standard.record_failure("A")
        assert r == {"status": "invalid", "attempts_to_next_lock": 2}

    def test_second_failure_one_remaining(self, lockouts_standard):
        lockouts_standard.record_failure("A")
        r = lockouts_standard.record_failure("A")
        assert r["status"] == "invalid"
        assert r["attempts_to_next_lock"] == 1

    def test_third_failure_triggers_timed_lock(self, lockouts_standard):
        _fail_n(lockouts_standard, "A", 2)
        r = lockouts_standard.record_failure("A")
        assert r["status"] == "locked"
        assert r["remaining_lock_seconds"] >= 0
        assert lockouts_standard.check("A")["status"] == "locked"

    def test_while_locked_check_returns_locked(self, lockouts_standard):
        _fail_n(lockouts_standard, "A", 3)
        c = lockouts_standard.check("A")
        assert c is not None
        assert c["status"] == "locked"


@pytest.mark.memory
class TestLockoutTiers:
    def test_six_failures_second_tier_lock(self, lockouts_standard, utc_now):
        ts = utc_now.timestamp()

        def fake_time():
            return ts

        with patch("lockouts._now", return_value=utc_now), patch("time.time", fake_time):
            _fail_n(lockouts_standard, "TIER", 6)
            c = lockouts_standard.check("TIER")
        assert c is not None
        assert c["status"] == "locked"

    def test_nine_failures_permanent_admin_lock(self, lockouts_standard, utc_now):
        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_standard, "PERM", 9)
            c = lockouts_standard.check("PERM")
        assert c["status"] == "locked"
        assert c.get("admin_unlock_required") is True

    def test_permanent_lock_record_failure_returns_admin_response(self, lockouts_standard, utc_now):
        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_standard, "PERM2", 9)
            r = lockouts_standard.record_failure("PERM2")
        assert r.get("admin_unlock_required") is True


@pytest.mark.memory
class TestLockoutExpiry:
    def test_expired_timed_lock_cleared_on_check(self, lockouts_fast, utc_now):
        ts = utc_now.timestamp()
        later_ts = (utc_now + timedelta(minutes=1)).timestamp()

        with patch("lockouts._now", return_value=utc_now), patch("time.time", return_value=ts):
            _fail_n(lockouts_fast, "EXP", 3)
            assert lockouts_fast.check("EXP")["status"] == "locked"
        with patch("lockouts._now", return_value=utc_now + timedelta(minutes=1)), patch(
            "time.time", return_value=later_ts
        ):
            assert lockouts_fast.check("EXP") is None

    def test_cleanup_expired_clears_memory_lock(self, lockouts_fast, utc_now):
        import lockouts as lo

        ts = utc_now.timestamp()
        later_ts = (utc_now + timedelta(minutes=1)).timestamp()

        with patch("lockouts._now", return_value=utc_now), patch("time.time", return_value=ts):
            _fail_n(lockouts_fast, "CLN", 3)
        with patch("lockouts._now", return_value=utc_now + timedelta(minutes=1)), patch(
            "time.time", return_value=later_ts
        ):
            n = lo.cleanup_expired()
        assert n >= 1


@pytest.mark.memory
class TestLockoutReset:
    def test_reset_after_failures_clears_state(self, lockouts_standard):
        _fail_n(lockouts_standard, "RST", 2)
        lockouts_standard.reset("RST")
        assert lockouts_standard.check("RST") is None

    def test_reset_does_not_clear_permanent_lock(self, lockouts_standard, utc_now):
        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_standard, "NOPERM", 9)
        lockouts_standard.reset("NOPERM")
        c = lockouts_standard.check("NOPERM")
        assert c is not None
        assert c.get("admin_unlock_required") is True


@pytest.mark.memory
class TestLockoutAdminAndPinReset:
    def test_admin_unlock_sets_pin_reset_required(self, lockouts_standard):
        lockouts_standard.admin_unlock("ADM1")
        assert lockouts_standard.check("ADM1") == {"status": "pin_reset_required"}
        assert lockouts_standard.requires_pin_reset("ADM1") is True

    def test_complete_pin_reset_clears_row(self, lockouts_standard):
        lockouts_standard.admin_unlock("ADM2")
        lockouts_standard.complete_pin_reset("ADM2")
        assert lockouts_standard.check("ADM2") is None
        assert not lockouts_standard.requires_pin_reset("ADM2")

    def test_admin_unlock_on_permanent_clears_counters(self, lockouts_standard, utc_now):
        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_standard, "ADM3", 9)
        lockouts_standard.admin_unlock("ADM3")
        assert lockouts_standard.check("ADM3")["status"] == "pin_reset_required"

    def test_reset_skips_must_reset_pin(self, lockouts_standard):
        lockouts_standard.admin_unlock("PIN1")
        lockouts_standard.reset("PIN1")
        assert lockouts_standard.requires_pin_reset("PIN1") is True


@pytest.mark.memory
class TestLockoutConfigure:
    def test_configure_respects_max_attempts(self):
        import lockouts

        lockouts.configure(2, [15, 30])
        lockouts.record_failure("CFG")
        r = lockouts.record_failure("CFG")
        assert r["status"] == "locked"
        lockouts.configure(3, [15, 30])

    def test_timed_lock_response_uses_datetime(self, lockouts_standard):
        import lockouts as lo

        until = datetime.now(timezone.utc) + timedelta(minutes=5)
        r = lo._timed_lock_response(until)
        assert r["status"] == "locked"
        assert r["remaining_lock_seconds"] >= 0

    def test_failure_response_on_block_boundary(self, lockouts_standard, utc_now):
        import lockouts as lo

        with patch("lockouts._now", return_value=utc_now):
            _fail_n(lockouts_standard, "BD", 3)
            r = lo._failure_response(3, utc_now + timedelta(minutes=15))
        assert r["status"] == "locked"

    def test_accounts_are_isolated(self, lockouts_standard):
        _fail_n(lockouts_standard, "ISO1", 3)
        assert lockouts_standard.check("ISO1") is not None
        assert lockouts_standard.check("ISO2") is None
