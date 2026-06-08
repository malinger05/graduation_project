"""
test_fraud_detection.py — unit tests for the middleware fraud-detection module.

Run from the directory that contains fraud_detection.py (the middleware dir):

    pytest test_fraud_detection.py -v

The suite exercises pure logic only — no real database, no network, no chain.
DB-touching paths (concurrent-session, persistence) are covered with monkeypatched
fakes so the tests stay fast and deterministic.

Design focus: the COLD-START guarantee. A brand-new cardholder, or a large first
transaction, must never be flagged as behaviourally suspicious. Those cases are
called out explicitly below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import fraud_detection as fd


# ── Fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test starts from a clean login-failure window and detection enabled."""
    fd._login_fails.clear()
    monkeypatch.setattr(config, "FRAUD_DETECTION_ENABLED", True, raising=False)
    yield
    fd._login_fails.clear()


@pytest.fixture
def lenient_universal(monkeypatch):
    """Neutralise the universal controls so behavioural checks can be tested
    in isolation (otherwise a high amount would trip velocity/limit/large-cash)."""
    monkeypatch.setattr(config, "FRAUD_VELOCITY_MAX_TXNS_PER_HOUR", 100000, raising=False)
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1e12, raising=False)
    monkeypatch.setattr(config, "FRAUD_LARGE_CASH_THRESHOLD", 1e12, raising=False)
    monkeypatch.setattr(config, "FRAUD_MAX_REVERSALS_24H", 100000, raising=False)


NOON = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)   # safely outside night window
NIGHT = datetime(2026, 5, 31, 3, 0, 0, tzinfo=timezone.utc)   # inside 00:00–05:00 window


def tx(amount, ttype="WITHDRAW", minutes_ago=120, dispense=None, *, now=NOON):
    """Build one history row in Core Banking's response shape."""
    if dispense is None:
        dispense = "DISPENSED" if ttype == "WITHDRAW" else "NOT_APPLICABLE"
    created = now - timedelta(minutes=minutes_ago)
    return {
        "amount": amount,
        "transactionType": ttype,
        # Spring Boot LocalDateTime: naive ISO, no offset
        "createdAt": created.strftime("%Y-%m-%dT%H:%M:%S"),
        "dispenseStatus": dispense,
    }


def codes(assessment):
    return {s.code for s in assessment.signals}


def severities(assessment):
    return {s.severity for s in assessment.signals}


# ── Result-type helpers ───────────────────────────────────────────────────────

def test_assessment_blocked_and_reason():
    a = fd.Assessment()
    a.add("x", fd.SEV_INFO, "info msg")
    a.add("y", fd.SEV_BLOCK, "blocked msg", k=1)
    assert a.blocked is True
    assert a.block_reason == "blocked msg"
    as_list = a.as_list()
    assert as_list[1] == {
        "code": "y", "severity": "block", "message": "blocked msg", "detail": {"k": 1}
    }


def test_assessment_not_blocked_when_only_info():
    a = fd.Assessment()
    a.add("x", fd.SEV_INFO, "info")
    a.add("y", fd.SEV_REVIEW, "review")
    assert a.blocked is False
    assert a.block_reason is None


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_DETECTION_ENABLED", False, raising=False)
    a = fd.assess_transaction(
        account_number="DE1", transaction_type="WITHDRAW",
        amount=999999, history=[tx(50) for _ in range(20)], now=NOON,
    )
    assert a.signals == []
    assert a.blocked is False


# ── COLD-START: the headline requirement ───────────────────────────────────────

def test_new_account_large_first_withdrawal_not_flagged(lenient_universal):
    """A first transaction on a brand-new card, even a large one, must not be
    blocked and must raise no behavioural signal."""
    a = fd.assess_transaction(
        account_number="DE_NEW", transaction_type="WITHDRAW",
        amount=5000, history=[], now=NOON,
    )
    assert a.new_account is True
    assert a.blocked is False
    assert not (codes(a) & {"amount_anomaly", "dormant_reactivation", "night_activity_pattern"})


def test_new_account_below_min_history_skips_behavioural(lenient_universal):
    """Fewer than FRAUD_MIN_HISTORY_FOR_ANOMALY completed txns ⇒ still 'new'."""
    history = [tx(10, minutes_ago=200 + i) for i in range(config.FRAUD_MIN_HISTORY_FOR_ANOMALY - 1)]
    a = fd.assess_transaction(
        account_number="DE_NEW", transaction_type="WITHDRAW",
        amount=99999, history=history, now=NOON,
    )
    assert a.new_account is True
    assert "amount_anomaly" not in codes(a)


def test_established_account_enables_behavioural(lenient_universal):
    history = [tx(100, minutes_ago=200 + i) for i in range(config.FRAUD_MIN_HISTORY_FOR_ANOMALY)]
    a = fd.assess_transaction(
        account_number="DE_OLD", transaction_type="WITHDRAW",
        amount=100, history=history, now=NOON,
    )
    assert a.new_account is False


def test_large_first_deposit_only_compliance_info():
    """A large first deposit yields a regulatory breadcrumb (info), never a block."""
    a = fd.assess_transaction(
        account_number="DE_NEW", transaction_type="DEPOSIT",
        amount=12000, history=[], now=NOON,
    )
    assert a.blocked is False
    assert "large_cash_report" in codes(a)
    sig = next(s for s in a.signals if s.code == "large_cash_report")
    assert sig.severity == fd.SEV_INFO
    assert sig.detail.get("regulatory") is True


def test_first_withdrawal_with_only_deposit_history_no_anomaly(lenient_universal):
    """Account has lots of deposits (so not 'new') but this is its FIRST withdrawal.
    Relative-amount needs same-direction samples, so no anomaly fires."""
    history = [tx(50, ttype="DEPOSIT", minutes_ago=300 + i) for i in range(8)]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=9000, history=history, now=NOON,
    )
    assert a.new_account is False
    assert "amount_anomaly" not in codes(a)


# ── Velocity (universal) ───────────────────────────────────────────────────────

def test_velocity_blocks_when_too_many_in_hour(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_VELOCITY_MAX_TXNS_PER_HOUR", 3, raising=False)
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1e12, raising=False)
    history = [tx(10, minutes_ago=m) for m in (5, 15, 30)]  # 3 in the last hour
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=10, history=history, now=NOON,
    )
    assert a.blocked is True
    assert a.block_reason and "last hour" in a.block_reason
    assert "velocity_txn_count" in codes(a)


def test_velocity_ignores_old_transactions(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_VELOCITY_MAX_TXNS_PER_HOUR", 3, raising=False)
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1e12, raising=False)
    history = [tx(10, minutes_ago=m) for m in (90, 120, 200, 300)]  # all > 1h ago
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=10, history=history, now=NOON,
    )
    assert "velocity_txn_count" not in codes(a)


# ── Daily withdrawal limit (universal) ─────────────────────────────────────────

def test_daily_limit_blocks_on_cumulative(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1000.0, raising=False)
    monkeypatch.setattr(config, "FRAUD_VELOCITY_MAX_TXNS_PER_HOUR", 100000, raising=False)
    history = [tx(400, minutes_ago=120), tx(400, minutes_ago=300)]  # 800 already today
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=300, history=history, now=NOON,  # 800 + 300 = 1100 > 1000
    )
    assert a.blocked is True
    assert "daily_withdrawal_limit" in codes(a)


def test_daily_limit_excludes_reversed(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1000.0, raising=False)
    monkeypatch.setattr(config, "FRAUD_VELOCITY_MAX_TXNS_PER_HOUR", 100000, raising=False)
    history = [tx(900, minutes_ago=120, dispense="REVERSED")]  # reversed ⇒ not counted
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=300, history=history, now=NOON,
    )
    assert "daily_withdrawal_limit" not in codes(a)
    assert a.blocked is False


def test_deposit_does_not_trigger_daily_withdrawal_limit(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1000.0, raising=False)
    a = fd.assess_transaction(
        account_number="DE", transaction_type="DEPOSIT",
        amount=5000, history=[], now=NOON,
    )
    assert "daily_withdrawal_limit" not in codes(a)


# ── Large cash compliance (universal, info only) ───────────────────────────────

def test_large_cash_reports_but_never_blocks(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_LARGE_CASH_THRESHOLD", 10000.0, raising=False)
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1e12, raising=False)
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=10000, history=[], now=NOON,
    )
    assert "large_cash_report" in codes(a)
    assert a.blocked is False


def test_below_large_cash_threshold_no_report(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_LARGE_CASH_THRESHOLD", 10000.0, raising=False)
    a = fd.assess_transaction(
        account_number="DE", transaction_type="DEPOSIT",
        amount=9999.99, history=[], now=NOON,
    )
    assert "large_cash_report" not in codes(a)


# ── Relative-amount anomaly (behavioural, gated) ───────────────────────────────

def test_amount_anomaly_when_far_above_personal_average(lenient_universal):
    history = [tx(100, minutes_ago=200 + i * 10) for i in range(5)]  # avg 100, 5 samples
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=400, history=history, now=NOON,  # 400 > 3 * 100
    )
    assert "amount_anomaly" in codes(a)
    assert severities(a) == {fd.SEV_REVIEW} | (severities(a) & {fd.SEV_INFO})
    assert a.blocked is False  # review never blocks


def test_amount_within_normal_range_no_anomaly(lenient_universal):
    history = [tx(100, minutes_ago=200 + i * 10) for i in range(5)]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=150, history=history, now=NOON,  # below 3x avg
    )
    assert "amount_anomaly" not in codes(a)


def test_anomaly_requires_min_samedirection_samples(lenient_universal):
    # 5 completed total (not new) but only 2 withdrawals → below ANOMALY_MIN_SAMPLES
    history = [tx(100, minutes_ago=200), tx(100, minutes_ago=210)] + \
              [tx(10, ttype="DEPOSIT", minutes_ago=220 + i) for i in range(3)]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=99999, history=history, now=NOON,
    )
    assert a.new_account is False
    assert "amount_anomaly" not in codes(a)


# ── Dormancy (behavioural, gated) ──────────────────────────────────────────────

def test_dormant_reactivation_flagged(lenient_universal, monkeypatch):
    monkeypatch.setattr(config, "FRAUD_DORMANCY_DAYS", 90, raising=False)
    old = 100 * 24 * 60  # 100 days ago, in minutes
    history = [tx(100, minutes_ago=old + i) for i in range(5)]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=100, history=history, now=NOON,
    )
    assert "dormant_reactivation" in codes(a)


def test_recent_activity_not_dormant(lenient_universal):
    history = [tx(100, minutes_ago=200 + i) for i in range(5)]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=100, history=history, now=NOON,
    )
    assert "dormant_reactivation" not in codes(a)


# ── Night activity: breadcrumb (universal) vs pattern (gated) ──────────────────

def test_night_breadcrumb_is_info_for_new_account():
    a = fd.assess_transaction(
        account_number="DE_NEW", transaction_type="WITHDRAW",
        amount=20, history=[], now=NIGHT,
    )
    assert "night_activity" in codes(a)
    assert next(s for s in a.signals if s.code == "night_activity").severity == fd.SEV_INFO
    # New account ⇒ no behavioural pattern signal
    assert "night_activity_pattern" not in codes(a)


def test_night_pattern_flagged_for_established_account(lenient_universal, monkeypatch):
    monkeypatch.setattr(config, "FRAUD_NIGHT_MAX_TXNS", 3, raising=False)
    # 5 completed (not new), 3 of them within the night window in the last 24h.
    # History timestamps MUST be built relative to the same reference time (NIGHT)
    # passed to assess_transaction, or they land outside the night window.
    history = [tx(100, minutes_ago=60, now=NIGHT), tx(100, minutes_ago=120, now=NIGHT),
               tx(100, minutes_ago=180, now=NIGHT), tx(100, minutes_ago=600, now=NIGHT),
               tx(100, minutes_ago=700, now=NIGHT)]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=100, history=history, now=NIGHT,
    )
    assert "night_activity_pattern" in codes(a)


# ── Repeated reversals (universal — abnormal even on a new account) ────────────

def test_repeated_reversals_flagged(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_MAX_REVERSALS_24H", 2, raising=False)
    monkeypatch.setattr(config, "FRAUD_DAILY_WITHDRAWAL_LIMIT", 1e12, raising=False)
    history = [tx(50, minutes_ago=30, dispense="REVERSED"),
               tx(50, minutes_ago=60, dispense="REVERSED")]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=50, history=history, now=NOON,
    )
    assert "repeated_reversals" in codes(a)


# ── Input coercion ─────────────────────────────────────────────────────────────

def test_amount_accepts_string_and_float(lenient_universal):
    for amount in ("100.00", 100.0, 100):
        a = fd.assess_transaction(
            account_number="DE", transaction_type="WITHDRAW",
            amount=amount, history=[], now=NOON,
        )
        assert a.blocked is False


def test_malformed_history_rows_ignored(lenient_universal):
    history = [None, "garbage", {"amount": "oops", "createdAt": "not-a-date"}]
    a = fd.assess_transaction(
        account_number="DE", transaction_type="WITHDRAW",
        amount=100, history=history, now=NOON,
    )
    assert isinstance(a, fd.Assessment)  # no crash
    assert a.new_account is True


# ── Login: cross-card brute force ──────────────────────────────────────────────

def test_cross_card_bruteforce_blocks_after_many_accounts(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_ACCOUNTS_PER_SOURCE_15M", 5, raising=False)
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_FAILS_PER_SOURCE_15M", 100, raising=False)
    for i in range(5):
        fd.record_login_failure(cert_serial="KIOSK-A", account_number=f"DE-{i}")
    a = fd.assess_login(account_number="DE-X", cert_serial="KIOSK-A")
    assert a.blocked is True
    assert "cross_card_bruteforce" in codes(a)


def test_cross_card_under_threshold_not_blocked(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_ACCOUNTS_PER_SOURCE_15M", 5, raising=False)
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_FAILS_PER_SOURCE_15M", 100, raising=False)
    for i in range(3):
        fd.record_login_failure(cert_serial="KIOSK-B", account_number=f"DE-{i}")
    a = fd.assess_login(account_number="DE-X", cert_serial="KIOSK-B")
    assert a.blocked is False


def test_login_failures_separate_per_terminal(monkeypatch):
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_ACCOUNTS_PER_SOURCE_15M", 3, raising=False)
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_FAILS_PER_SOURCE_15M", 100, raising=False)
    for i in range(3):
        fd.record_login_failure(cert_serial="KIOSK-A", account_number=f"DE-{i}")
    # A different terminal is unaffected
    a = fd.assess_login(account_number="DE-X", cert_serial="KIOSK-CLEAN")
    assert a.blocked is False


def test_login_window_expiry(monkeypatch):
    """Failures older than 15 minutes are pruned."""
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_ACCOUNTS_PER_SOURCE_15M", 3, raising=False)
    monkeypatch.setattr(config, "FRAUD_LOGIN_MAX_FAILS_PER_SOURCE_15M", 100, raising=False)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(fd, "time", SimpleNamespace(time=lambda: clock["t"]))
    for i in range(3):
        fd.record_login_failure(cert_serial="KIOSK-A", account_number=f"DE-{i}")
    clock["t"] += 16 * 60  # advance past the 15-minute window
    a = fd.assess_login(account_number="DE-X", cert_serial="KIOSK-A")
    assert a.blocked is False


# ── Login: concurrent session (DB-backed, monkeypatched) ───────────────────────

def test_concurrent_session_flagged(monkeypatch):
    class _FakeSession:
        def scalars(self, *_a, **_k):
            return SimpleNamespace(all=lambda: [object(), object()])  # 2 active

    class _CM:
        def __enter__(self): return _FakeSession()
        def __exit__(self, *a): return False

    monkeypatch.setattr(fd.db, "is_enabled", lambda: True)
    monkeypatch.setattr(fd.db, "db_session", lambda: _CM())
    # select(...).where(...) must be chainable; the fake session ignores the arg.
    _query = SimpleNamespace(where=lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(fd, "select", lambda *a, **k: _query)

    a = fd.assess_login(account_number="DE", cert_serial="KIOSK-FRESH")
    assert "concurrent_session" in codes(a)
    assert a.blocked is False  # review only


def test_no_concurrent_session_when_db_disabled(monkeypatch):
    monkeypatch.setattr(fd.db, "is_enabled", lambda: False)
    a = fd.assess_login(account_number="DE", cert_serial="KIOSK-FRESH")
    assert "concurrent_session" not in codes(a)


# ── Persistence (record_assessment) ────────────────────────────────────────────

def test_record_assessment_noop_when_db_disabled(monkeypatch):
    monkeypatch.setattr(fd.db, "is_enabled", lambda: False)
    a = fd.Assessment()
    a.add("x", fd.SEV_REVIEW, "msg")
    # must not raise
    fd.record_assessment(account_number="DE", endpoint="/atm/withdraw", assessment=a)


def test_record_assessment_writes_event(monkeypatch):
    saved = []

    class _FakeSession:
        def add(self, obj): saved.append(obj)

    class _CM:
        def __enter__(self): return _FakeSession()
        def __exit__(self, *a): return False

    monkeypatch.setattr(fd.db, "is_enabled", lambda: True)
    monkeypatch.setattr(fd.db, "db_session", lambda: _CM())

    a = fd.Assessment(new_account=True)
    a.add("velocity_txn_count", fd.SEV_BLOCK, "too many")
    fd.record_assessment(
        account_number="DE", endpoint="/atm/withdraw", assessment=a, correlation_id="c1"
    )
    assert len(saved) == 1
    ev = saved[0]
    assert ev.severity == "block"
    assert ev.blocked is True
    assert ev.new_account is True
    assert ev.account_number == "DE"
    assert ev.correlation_id == "c1"


def test_record_assessment_skips_empty(monkeypatch):
    saved = []
    monkeypatch.setattr(fd.db, "is_enabled", lambda: True)
    monkeypatch.setattr(fd.db, "db_session", lambda: (_ for _ in ()).throw(AssertionError("should not open")))
    fd.record_assessment(account_number="DE", endpoint="/atm/withdraw", assessment=fd.Assessment())
    assert saved == []  # nothing persisted, db_session never called
