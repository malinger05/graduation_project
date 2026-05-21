"""
lockouts.py  —  Login failure / account lockout state for the middleware.

Progressive lockout policy:
  - Lock out after every 3 consecutive invalid authentication attempts.
  - Timed tiers: 15 minutes, then 30 minutes (9 failures total before permanent).
  - After the 30-minute tier, 3 more failures → admin-unlock-only (permanent).

When MIDDLEWARE_DB_URL is set, state lives in login_lockouts and survives
middleware restarts. When unset, falls back to an in-memory dict.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import db
from models import LoginLockout

_max_attempts: int = 3
_lockout_minutes: list[int] = [15, 30]

_memory: dict[str, dict] = {}
_memory_lock = threading.Lock()


def configure(max_attempts: int, lockout_minutes: list[int]) -> None:
    global _max_attempts, _lockout_minutes
    _max_attempts = max(1, max_attempts)
    _lockout_minutes = lockout_minutes or [15, 30]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """Normalize DB timestamps for safe comparison with aware UTC now()."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _admin_lock_response() -> dict:
    return {
        "status":                   "locked",
        "admin_unlock_required":    True,
        "remaining_lock_seconds":   0,
        "lock_minutes":             0,
    }


def _pin_reset_response() -> dict:
    return {"status": "pin_reset_required"}


def _timed_lock_response(locked_until: datetime | float) -> dict:
    if isinstance(locked_until, datetime):
        remaining = max(0, int((_as_utc(locked_until) - _now()).total_seconds()))
    else:
        remaining = max(0, int(locked_until - time.time()))
    lock_minutes = max(1, remaining // 60) if remaining else 1
    return {
        "status":                 "locked",
        "remaining_lock_seconds": remaining,
        "lock_minutes":           lock_minutes,
    }


def _failure_response(failed: int, locked_until: datetime | None) -> dict:
    if failed % _max_attempts == 0 and locked_until is not None:
        return _timed_lock_response(locked_until)
    attempts_to_next = _max_attempts - (failed % _max_attempts)
    return {"status": "invalid", "attempts_to_next_lock": attempts_to_next}


def _expire_timed_lock(row: LoginLockout) -> None:
    """Clear an expired timed lock and reset attempt counter."""
    row.locked_until    = None
    row.failed_attempts = 0
    row.updated_at      = _now()


def _expire_timed_lock_memory(entry: dict) -> None:
    entry["locked_until"]    = None
    entry["failed_attempts"] = 0


def _apply_timed_or_permanent_lock(
    lock_tier: int,
    *,
    set_tier,
    set_locked_until,
    set_permanent,
) -> dict:
    """Apply timed lockout by tier, then require admin unlock."""
    if lock_tier >= len(_lockout_minutes):
        set_permanent(True)
        set_locked_until(None)
        return _admin_lock_response()

    minutes = _lockout_minutes[lock_tier]
    set_tier(lock_tier + 1)
    until = _now() + timedelta(minutes=minutes)
    set_locked_until(until)
    return _timed_lock_response(until)


def check(account_number: str) -> dict | None:
    """Return lockout info if the account is locked, else None."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is None:
                return None
            if row.must_reset_pin:
                return _pin_reset_response()
            if row.permanently_locked:
                return _admin_lock_response()
            if row.locked_until and _as_utc(row.locked_until) > _now():
                return _timed_lock_response(row.locked_until)
            if row.locked_until and _as_utc(row.locked_until) <= _now():
                _expire_timed_lock(row)
            return None

    with _memory_lock:
        entry = _memory.get(account_number)
        if not entry:
            return None
        if entry.get("must_reset_pin"):
            return _pin_reset_response()
        if entry.get("permanently_locked"):
            return _admin_lock_response()
        locked_until = entry.get("locked_until")
        if locked_until and time.time() < locked_until:
            return _timed_lock_response(locked_until)
        if locked_until and time.time() >= locked_until:
            _expire_timed_lock_memory(entry)
        return None


def record_failure(account_number: str) -> dict:
    """Increment failure counter and apply timed lockout on threshold."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is None:
                row = LoginLockout(
                    account_number     = account_number,
                    failed_attempts    = 0,
                    locked_until       = None,
                    lock_tier          = 0,
                    permanently_locked = False,
                    must_reset_pin     = False,
                    updated_at         = _now(),
                )
                s.add(row)
                s.flush()
            if row.permanently_locked:
                return _admin_lock_response()

            row.failed_attempts += 1
            row.updated_at = _now()
            failed = row.failed_attempts

            if failed % _max_attempts == 0:
                def set_tier(v: int) -> None:
                    row.lock_tier = v

                def set_locked_until(v: datetime | None) -> None:
                    row.locked_until = v

                def set_permanent(v: bool) -> None:
                    row.permanently_locked = v

                result = _apply_timed_or_permanent_lock(
                    row.lock_tier,
                    set_tier=set_tier,
                    set_locked_until=set_locked_until,
                    set_permanent=set_permanent,
                )
                return result

            return _failure_response(failed, row.locked_until)

    with _memory_lock:
        entry = _memory.setdefault(
            account_number,
            {
                "failed_attempts":    0,
                "locked_until":       None,
                "lock_tier":          0,
                "permanently_locked": False,
                "must_reset_pin":     False,
            },
        )
        if entry.get("permanently_locked"):
            return _admin_lock_response()

        entry["failed_attempts"] += 1
        failed = entry["failed_attempts"]

        if failed % _max_attempts == 0:
            tier = entry["lock_tier"]

            def set_tier(v: int) -> None:
                entry["lock_tier"] = v

            def set_locked_until(v: datetime | None) -> None:
                if v is None:
                    entry["locked_until"] = None
                else:
                    entry["locked_until"] = v.timestamp()

            def set_permanent(v: bool) -> None:
                entry["permanently_locked"] = v

            return _apply_timed_or_permanent_lock(
                tier,
                set_tier=set_tier,
                set_locked_until=set_locked_until,
                set_permanent=set_permanent,
            )

        attempts_to_next = _max_attempts - (failed % _max_attempts)
        return {"status": "invalid", "attempts_to_next_lock": attempts_to_next}


def reset(account_number: str) -> None:
    """Clear lockout state after a successful login."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is not None and not row.permanently_locked and not row.must_reset_pin:
                s.delete(row)
        return

    with _memory_lock:
        entry = _memory.get(account_number)
        if entry and not entry.get("permanently_locked") and not entry.get("must_reset_pin"):
            _memory.pop(account_number, None)


def admin_unlock(account_number: str) -> None:
    """
    Staff unlock after identity check: clear lock counters; customer must set a new PIN.
    Creates a lockout row with must_reset_pin if none exists.
    """
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is None:
                row = LoginLockout(
                    account_number     = account_number,
                    failed_attempts    = 0,
                    locked_until       = None,
                    lock_tier          = 0,
                    permanently_locked = False,
                    must_reset_pin     = True,
                    updated_at         = _now(),
                )
                s.add(row)
            else:
                row.failed_attempts    = 0
                row.lock_tier          = 0
                row.locked_until       = None
                row.permanently_locked = False
                row.must_reset_pin     = True
                row.updated_at         = _now()
        return

    with _memory_lock:
        entry = _memory.get(account_number)
        if entry is None:
            _memory[account_number] = {
                "failed_attempts":    0,
                "locked_until":       None,
                "lock_tier":          0,
                "permanently_locked": False,
                "must_reset_pin":     True,
            }
        else:
            entry["failed_attempts"]    = 0
            entry["lock_tier"]          = 0
            entry["locked_until"]       = None
            entry["permanently_locked"] = False
            entry["must_reset_pin"]     = True


def requires_pin_reset(account_number: str) -> bool:
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            return row is not None and bool(row.must_reset_pin)
    with _memory_lock:
        entry = _memory.get(account_number)
        return bool(entry and entry.get("must_reset_pin"))


def complete_pin_reset(account_number: str) -> None:
    """Remove lockout row after customer sets a new PIN."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is not None:
                s.delete(row)
        return
    with _memory_lock:
        _memory.pop(account_number, None)


def cleanup_expired() -> int:
    """Clear expired timed lockouts. Returns rows cleared. Skips permanent locks."""
    now = _now()

    if db.is_enabled():
        from sqlalchemy import select

        with db.db_session() as s:
            rows = list(
                s.scalars(
                    select(LoginLockout).where(
                        LoginLockout.permanently_locked.is_(False),
                        LoginLockout.locked_until.isnot(None),
                        LoginLockout.locked_until < now,
                    )
                ).all()
            )
            for row in rows:
                _expire_timed_lock(row)
            return len(rows)

    cleared = 0
    with _memory_lock:
        for entry in _memory.values():
            if entry.get("permanently_locked"):
                continue
            locked_until = entry.get("locked_until")
            if locked_until and time.time() >= locked_until:
                _expire_timed_lock_memory(entry)
                cleared += 1
    return cleared
