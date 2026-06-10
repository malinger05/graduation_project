"""
fraud_detection.py  —  Layer 2 fraud & compliance signal evaluation.

Runs inside the middleware, AROUND the Core Banking call for a deposit or
withdraw, and at login. It owns no banking state: it reads transaction history
from Core Banking (the source of truth) and the middleware's own session table.

Two outcome categories:
  - decision "block"  → middleware rejects the request (HTTP 403)
  - signals (severity "review"/"info") → attached to the audit row and the
    fraud_events table for an analyst; the transaction still proceeds.

COLD-START SAFETY (important — read this before tuning):
  A brand-new cardholder has little or no history. Checks that compare a
  transaction to the customer's OWN past behaviour — relative-amount anomaly,
  dormancy reactivation, repeated overnight pattern — are SUPPRESSED until the
  account has at least FRAUD_MIN_HISTORY_FOR_ANOMALY completed transactions.
  A first transaction, or a legitimately large first withdrawal, therefore
  never produces a "suspicious" signal merely because there is no baseline.

  Controls that are fair regardless of account age still apply to everyone,
  including new accounts, because they are LIMITS or COMPLIANCE breadcrumbs,
  not behavioural accusations:
    - per-hour transaction velocity count
    - fixed daily cash-withdrawal limit
    - regulatory large-cash reporting (info only, never blocks)
    - dispense-reversal abuse (abnormal even on day one)
    - login brute-force spread across many cards from one terminal
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

import config
import db
from models import FraudEvent, SessionState

try:
    from sqlalchemy import select
except Exception:  # pragma: no cover - sqlalchemy always present at runtime
    select = None  # type: ignore[assignment]


# ── Severity levels ───────────────────────────────────────────────────────────
SEV_BLOCK = "block"    # request is rejected (HTTP 403)
SEV_REVIEW = "review"  # transaction proceeds; an analyst should review it
SEV_INFO = "info"      # informational / compliance breadcrumb only


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class Signal:
    code: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class Assessment:
    signals: list[Signal] = field(default_factory=list)
    new_account: bool = False

    @property
    def blocked(self) -> bool:
        return any(s.severity == SEV_BLOCK for s in self.signals)

    @property
    def block_reason(self) -> Optional[str]:
        for s in self.signals:
            if s.severity == SEV_BLOCK:
                return s.message
        return None

    def add(self, code: str, severity: str, message: str, **detail: Any) -> None:
        self.signals.append(Signal(code, severity, message, detail))

    def as_list(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.signals]


# ── Small helpers ─────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_decimal(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _parse_dt(raw: Any) -> Optional[datetime]:
    """Parse Core Banking timestamps (Spring Boot LocalDateTime, no offset)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:  # truncate fractional seconds / trailing chars and retry once
        dt = datetime.fromisoformat(s[:19])
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class _Txn:
    amount: Decimal
    type: str
    created_at: Optional[datetime]
    dispense_status: str


def _normalize_history(history: Iterable[dict] | None) -> list[_Txn]:
    out: list[_Txn] = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        out.append(
            _Txn(
                amount=_to_decimal(row.get("amount", 0)),
                type=str(row.get("transactionType") or row.get("type") or "").upper(),
                created_at=_parse_dt(row.get("createdAt") or row.get("created_at")),
                dispense_status=str(row.get("dispenseStatus") or "").upper(),
            )
        )
    return out


def _is_new_account(txns: list[_Txn]) -> bool:
    """
    A cardholder is treated as 'new' until they have enough completed history
    to establish a behavioural baseline. New accounts skip the behavioural
    checks, so a first or large transaction is never flagged as suspicious.
    """
    completed = [t for t in txns if t.dispense_status != "REVERSED"]
    return len(completed) < config.FRAUD_MIN_HISTORY_FOR_ANOMALY


def _in_night_window(dt: datetime) -> bool:
    start = config.FRAUD_NIGHT_START_HOUR
    end = config.FRAUD_NIGHT_END_HOUR
    h = dt.hour
    return (start <= h < end) if start < end else (h >= start or h < end)


# ── Transaction-time assessment ────────────────────────────────────────────────

def assess_transaction(
    *,
    account_number: str,
    transaction_type: str,
    amount: Any,
    history: Iterable[dict] | None,
    now: Optional[datetime] = None,
) -> Assessment:
    """
    Evaluate a deposit/withdraw before it is forwarded to Core Banking.
    `history` is the account's existing transactions (the current one excluded).
    """
    a = Assessment()
    if not config.FRAUD_DETECTION_ENABLED:
        return a

    ts = now or _now()
    amt = _to_decimal(amount)
    ttype = (transaction_type or "").upper()
    txns = _normalize_history(history)
    a.new_account = _is_new_account(txns)

    # ── Universal controls (fair regardless of account age) ────────────────────
    _check_velocity_count(a, txns, ts)
    if ttype == "WITHDRAW":
        _check_daily_withdrawal_limit(a, txns, amt, ts)
    _check_large_cash(a, ttype, amt)          # compliance breadcrumb (info)
    _check_night_breadcrumb(a, ts)            # info only — not suspicious alone
    _check_repeated_reversals(a, txns, ts)    # abnormal even on a new account

    # ── Behavioural checks (suppressed entirely for new accounts) ──────────────
    if not a.new_account:
        _check_relative_amount(a, txns, ttype, amt)
        _check_dormancy(a, txns, ts)
        _check_night_pattern(a, txns, ts)

    return a


# ── ISO/IEC 27035 — Velocity Controls  ────────────────────────────────────────
# Track how many transactions an account performs in a rolling time window. 
# Flag or block if thresholds are exceeded. 
# Specifically: more than N withdrawals per hour, total amount withdrawn exceeding a daily limit, 
# more than M failed PIN attempts from different IPs (already partially done via lockouts but not cross-IP).
def _check_velocity_count(a: Assessment, txns: list[_Txn], ts: datetime) -> None:
    window = ts - timedelta(hours=1)
    recent = [t for t in txns if t.created_at and t.created_at >= window]
    limit = config.FRAUD_VELOCITY_MAX_TXNS_PER_HOUR
    if limit > 0 and len(recent) >= limit:
        a.add(
            "velocity_txn_count", SEV_BLOCK,
            f"Too many transactions in the last hour ({len(recent)} >= {limit}).",
            count=len(recent), limit=limit, window_hours=1,
        )


def _check_daily_withdrawal_limit(
    a: Assessment, txns: list[_Txn], amt: Decimal, ts: datetime
) -> None:
    window = ts - timedelta(hours=24)
    spent = sum(
        (
            t.amount
            for t in txns
            if t.type == "WITHDRAW"
            and t.dispense_status != "REVERSED"
            and t.created_at
            and t.created_at >= window
        ),
        Decimal(0),
    )
    limit = _to_decimal(config.FRAUD_DAILY_WITHDRAWAL_LIMIT)
    if limit > 0 and (spent + amt) > limit:
        a.add(
            "daily_withdrawal_limit", SEV_BLOCK,
            f"Daily cash withdrawal limit exceeded "
            f"(already {spent} + {amt} > {limit}).",
            already_withdrawn=str(spent), attempted=str(amt), limit=str(limit),
        )


# ── FinCEN SAR-triggering thresholds — Large Cash Transaction ────────────────────────────────────────────────
# US/EU regulations require reporting of cash transactions above $10,000 (US) or €10,000 (EU). 
# In this system, flag any single deposit or withdrawal above the configured threshold and 
# write a large_cash_transaction signal to the fraud_signals column.
def _check_large_cash(a: Assessment, ttype: str, amt: Decimal) -> None:
    """Regulatory large-cash reporting (CTR-style). Informational, never blocks,
    never relative to the customer — so a large first deposit is fine."""
    threshold = _to_decimal(config.FRAUD_LARGE_CASH_THRESHOLD)
    if threshold > 0 and amt >= threshold and ttype in ("DEPOSIT", "WITHDRAW"):
        a.add(
            "large_cash_report", SEV_INFO,
            f"{ttype.title()} of {amt} meets the regulatory large-cash "
            f"reporting threshold ({threshold}). Compliance breadcrumb only.",
            amount=str(amt), threshold=str(threshold), regulatory=True,
        )


# ── PCI DSS v4.0 Requirement 10 — Suspicious Time-of-Day Detection  ────────────────────────────────────────────────────────────
# ATM activity between 00:00 and 05:00 local time that exceeds a threshold (e.g. more than 2 withdrawals in this window) is a known 
# fraud pattern (card skimming + overnight cash-out). Flag these transactions in the audit log with a fraud_signals JSON field.
def _check_night_breadcrumb(a: Assessment, ts: datetime) -> None:
    if _in_night_window(ts):
        a.add(
            "night_activity", SEV_INFO,
            f"Transaction at {ts.strftime('%H:%M')} UTC is within the overnight "
            f"window ({config.FRAUD_NIGHT_START_HOUR:02d}:00-"
            f"{config.FRAUD_NIGHT_END_HOUR:02d}:00).",
            hour=ts.hour,
        )



def _check_repeated_reversals(a: Assessment, txns: list[_Txn], ts: datetime) -> None:
    window = ts - timedelta(hours=24)
    reversed_ = [
        t for t in txns
        if t.dispense_status == "REVERSED" and t.created_at and t.created_at >= window
    ]
    limit = config.FRAUD_MAX_REVERSALS_24H
    if limit > 0 and len(reversed_) >= limit:
        a.add(
            "repeated_reversals", SEV_REVIEW,
            f"{len(reversed_)} dispense reversals in the last 24h (threshold "
            f"{limit}). Possible probing of the dispense-reversal flow.",
            count=len(reversed_), limit=limit,
        )


# ── FATF Recommendation 10 — Unusual Transaction Monitoring ────────────────────────────────────────────────────
# Flag transactions that are unusual relative to the customer's own history. 
# The simplest form: if the current withdrawal amount is more than 3× the customer's average withdrawal, flag it. 
# More advanced: flag first transaction on an account that has been dormant for 90+ days.
def _check_relative_amount(
    a: Assessment, txns: list[_Txn], ttype: str, amt: Decimal
) -> None:
    """Compare against the customer's own average for the SAME direction.
    Requires a minimum number of same-type samples, so the first withdrawal of
    an account that has only deposits is never flagged."""
    same = [
        t.amount for t in txns
        if t.type == ttype and t.dispense_status != "REVERSED" and t.amount > 0
    ]
    if len(same) < config.FRAUD_ANOMALY_MIN_SAMPLES:
        return
    avg = sum(same, Decimal(0)) / Decimal(len(same))
    if avg <= 0:
        return
    mult = _to_decimal(config.FRAUD_ANOMALY_MULTIPLIER)
    if amt > avg * mult:
        a.add(
            "amount_anomaly", SEV_REVIEW,
            f"{ttype.title()} of {amt} is more than {mult}x the customer's "
            f"average {ttype.lower()} ({avg:.2f}).",
            amount=str(amt), average=f"{avg:.2f}", multiplier=str(mult),
            samples=len(same),
        )


def _check_dormancy(a: Assessment, txns: list[_Txn], ts: datetime) -> None:
    dated = [t.created_at for t in txns if t.created_at]
    if not dated:
        return
    gap = (ts - max(dated)).days
    threshold = config.FRAUD_DORMANCY_DAYS
    if threshold > 0 and gap >= threshold:
        a.add(
            "dormant_reactivation", SEV_REVIEW,
            f"Account reactivated after {gap} days of inactivity "
            f"(threshold {threshold}).",
            inactive_days=gap, threshold=threshold,
        )


def _check_night_pattern(a: Assessment, txns: list[_Txn], ts: datetime) -> None:
    window = ts - timedelta(hours=24)
    night = [
        t for t in txns
        if t.created_at and t.created_at >= window and _in_night_window(t.created_at)
    ]
    limit = config.FRAUD_NIGHT_MAX_TXNS
    if limit > 0 and len(night) >= limit:
        a.add(
            "night_activity_pattern", SEV_REVIEW,
            f"Repeated overnight activity: {len(night)} transactions in the "
            f"overnight window in the last 24h (threshold {limit}).",
            count=len(night), limit=limit,
        )


# ── Login-time assessment ───────────────────────────────────────────────────-

_login_fails: dict[str, list[tuple[float, str]]] = {}  # source -> [(ts, account)]
_login_lock = threading.Lock()


def _login_fail_window_seconds() -> float:
    minutes = max(1, config.FRAUD_LOGIN_FAIL_WINDOW_MINUTES)
    return minutes * 60.0


def _cert_key(cert_serial: Optional[str]) -> str:
    return (cert_serial or "unknown").strip() or "unknown"


def _prune_bucket(key: str, now: float) -> list[tuple[float, str]]:
    cutoff = now - _login_fail_window_seconds()
    with _login_lock:
        bucket = [(t, acc) for (t, acc) in _login_fails.get(key, []) if t >= cutoff]
        _login_fails[key] = bucket
        return list(bucket)


def terminal_lock_status(cert_serial: Optional[str]) -> dict | None:
    """
    Return terminal-wide login lock info when this kiosk exceeded the failed-login
    threshold inside the rolling window. None when logins are allowed.
    """
    if not config.FRAUD_DETECTION_ENABLED:
        return None

    key = _cert_key(cert_serial)
    now = time.time()
    bucket = _prune_bucket(key, now)
    distinct_accounts = {acc for _, acc in bucket}
    total = len(bucket)
    max_fails = config.FRAUD_LOGIN_MAX_FAILS_PER_SOURCE_15M
    max_accounts = config.FRAUD_LOGIN_MAX_ACCOUNTS_PER_SOURCE_15M
    over_fail_limit = max_fails > 0 and total >= max_fails
    over_account_limit = max_accounts > 0 and len(distinct_accounts) >= max_accounts
    if not over_fail_limit and not over_account_limit:
        return None

    oldest = min(t for t, _ in bucket)
    remaining = max(1, int(oldest + _login_fail_window_seconds() - now))
    window_min = config.FRAUD_LOGIN_FAIL_WINDOW_MINUTES
    if over_account_limit:
        message = (
            f"Too many accounts were tried on this ATM in the last {window_min} minutes. "
            f"Please wait before trying again."
        )
    else:
        message = (
            f"Too many incorrect PIN attempts on this ATM in the last {window_min} minutes. "
            f"Please wait before trying again."
        )
    return {
        "remaining_lock_seconds": remaining,
        "lock_minutes": max(1, (remaining + 59) // 60),
        "message": message,
        "terminal_lock": True,
    }


def record_login_failure(*, cert_serial: Optional[str], account_number: str) -> None:
    """
    Track a failed login keyed by client-cert serial (the kiosk's identity).
    Used to detect a single terminal brute-forcing PINs across many cards —
    a gap that neither the per-account lockout nor the per-IP rate limiter
    closes on their own.

    In-memory by design (the middleware runs as a single process here). For a
    multi-worker deployment back this with Redis.
    """
    key = _cert_key(cert_serial)
    now = time.time()
    with _login_lock:
        cutoff = now - _login_fail_window_seconds()
        bucket = [(t, acc) for (t, acc) in _login_fails.get(key, []) if t >= cutoff]
        bucket.append((now, account_number))
        _login_fails[key] = bucket


def assess_login(
    *,
    account_number: str,
    cert_serial: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Assessment:
    a = Assessment()
    if not config.FRAUD_DETECTION_ENABLED:
        return a
    _check_cross_card_bruteforce(a, cert_serial)
    _check_concurrent_session(a, account_number, now or _now())
    return a


def _check_cross_card_bruteforce(a: Assessment, cert_serial: Optional[str]) -> None:
    terminal = terminal_lock_status(cert_serial)
    if terminal is None:
        return
    key = _cert_key(cert_serial)
    bucket = _prune_bucket(key, time.time())
    distinct_accounts = {acc for _, acc in bucket}
    a.add(
        "cross_card_bruteforce", SEV_BLOCK,
        terminal["message"],
        failures_in_window=len(bucket),
        distinct_accounts=len(distinct_accounts),
        cert_serial=key,
        remaining_lock_seconds=terminal["remaining_lock_seconds"],
    )


# ── EMV 3-D Secure / Card-Not-Present pattern — Concurrent session detection ──────────────────────────────────
# If the same card number is used to initiate a login while a session for that account is already active 
# from a different source (different cert serial or channel), flag it as a possible cloned card attack. 
# The middleware's session_state table already has the card_number column — this is straightforward to check.
def _check_concurrent_session(a: Assessment, account_number: str, ts: datetime) -> None:
    if not db.is_enabled() or select is None:
        return
    cutoff = ts - timedelta(seconds=config.SESSION_TTL_SECONDS)
    try:
        with db.db_session() as s:
            rows = s.scalars(
                select(SessionState).where(
                    SessionState.account_number == account_number,
                    SessionState.last_active >= cutoff,
                )
            ).all()
            count = len(rows)
    except Exception:
        return
    if count >= 1:
        a.add(
            "concurrent_session", SEV_REVIEW,
            f"Login while {count} active session(s) already exist for this "
            f"account. Possible cloned card or shared credentials.",
            active_sessions=count,
        )


# ── Persistence ───────────────────────────────────────────────────────────────

def record_assessment(
    *,
    account_number: Optional[str],
    endpoint: str,
    assessment: Assessment,
    correlation_id: Optional[str] = None,
) -> None:
    """Persist a non-empty assessment for analyst review. No-op when the DB is
    disabled or there is nothing to record."""
    if not assessment.signals or not db.is_enabled():
        return
    if assessment.blocked:
        severity = SEV_BLOCK
    elif any(s.severity == SEV_REVIEW for s in assessment.signals):
        severity = SEV_REVIEW
    else:
        severity = SEV_INFO
    try:
        with db.db_session() as s:
            s.add(
                FraudEvent(
                    event_id=uuid.uuid4().hex,
                    correlation_id=correlation_id,
                    created_at=_now(),
                    account_number=account_number,
                    endpoint=endpoint,
                    severity=severity,
                    blocked=assessment.blocked,
                    new_account=assessment.new_account,
                    signals=assessment.as_list(),
                )
            )
    except Exception as exc:  # pragma: no cover
        print(f"[Fraud] failed to persist assessment: {exc}")
