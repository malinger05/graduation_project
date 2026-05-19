"""
lockouts.py  —  Login failure / account lockout state for the middleware.

Tracks failed PIN attempts and progressive lockout windows per account.
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
_lockout_minutes: list[int] = [5, 10, 15]

_memory: dict[str, dict] = {}
_memory_lock = threading.Lock()


def configure(max_attempts: int, lockout_minutes: list[int]) -> None:
    global _max_attempts, _lockout_minutes
    _max_attempts = max(1, max_attempts)
    _lockout_minutes = lockout_minutes or [5, 10, 15]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _failure_response(failed: int, locked_until: datetime | None) -> dict:
    if failed % _max_attempts == 0 and locked_until is not None:
        remaining = max(0, int((locked_until - _now()).total_seconds()))
        lock_minutes = max(1, remaining // 60)
        return {
            "status":                   "locked",
            "remaining_lock_seconds":   remaining,
            "lock_minutes":             lock_minutes,
        }
    attempts_to_next = _max_attempts - (failed % _max_attempts)
    return {"status": "invalid", "attempts_to_next_lock": attempts_to_next}


def check(account_number: str) -> dict | None:
    """Return lockout info if the account is locked, else None."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is None:
                return None
            if row.locked_until and row.locked_until > _now():
                remaining = int((row.locked_until - _now()).total_seconds())
                return {
                    "remaining_lock_seconds": remaining,
                    "lock_minutes":             remaining // 60,
                }
            if row.locked_until and row.locked_until <= _now():
                row.locked_until    = None
                row.failed_attempts = 0
                row.updated_at      = _now()
            return None

    with _memory_lock:
        entry = _memory.get(account_number)
        if not entry:
            return None
        locked_until = entry.get("locked_until")
        if locked_until and time.time() < locked_until:
            remaining = int(locked_until - time.time())
            return {
                "remaining_lock_seconds": remaining,
                "lock_minutes":             remaining // 60,
            }
        if locked_until and time.time() >= locked_until:
            entry["locked_until"]    = None
            entry["failed_attempts"] = 0
        return None


def record_failure(account_number: str) -> dict:
    """Increment failure counter; apply progressive lockout when threshold hit."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is None:
                row = LoginLockout(
                    account_number  = account_number,
                    failed_attempts = 0,
                    locked_until    = None,
                    updated_at      = _now(),
                )
                s.add(row)
            row.failed_attempts += 1
            row.updated_at = _now()
            failed = row.failed_attempts

            if failed % _max_attempts == 0:
                level        = min(failed // _max_attempts, len(_lockout_minutes)) - 1
                lock_minutes = _lockout_minutes[level]
                row.locked_until = _now() + timedelta(minutes=lock_minutes)

            return _failure_response(failed, row.locked_until)

    with _memory_lock:
        entry = _memory.setdefault(
            account_number, {"failed_attempts": 0, "locked_until": None}
        )
        entry["failed_attempts"] += 1
        failed = entry["failed_attempts"]

        if failed % _max_attempts == 0:
            level        = min(failed // _max_attempts, len(_lockout_minutes)) - 1
            lock_minutes = _lockout_minutes[level]
            entry["locked_until"] = time.time() + (lock_minutes * 60)
            return {
                "status":                 "locked",
                "remaining_lock_seconds": lock_minutes * 60,
                "lock_minutes":           lock_minutes,
            }

        attempts_to_next = _max_attempts - (failed % _max_attempts)
        return {"status": "invalid", "attempts_to_next_lock": attempts_to_next}


def reset(account_number: str) -> None:
    """Clear lockout state after a successful login."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(LoginLockout, account_number)
            if row is not None:
                s.delete(row)
        return

    with _memory_lock:
        _memory.pop(account_number, None)


def cleanup_expired() -> int:
    """Clear lockouts whose locked_until is in the past. Returns rows cleared."""
    now = _now()

    if db.is_enabled():
        from sqlalchemy import select

        with db.db_session() as s:
            rows = list(
                s.scalars(
                    select(LoginLockout).where(
                        LoginLockout.locked_until.isnot(None),
                        LoginLockout.locked_until < now,
                    )
                ).all()
            )
            for row in rows:
                row.locked_until    = None
                row.failed_attempts = 0
                row.updated_at      = now
            return len(rows)

    cleared = 0
    with _memory_lock:
        for entry in _memory.values():
            locked_until = entry.get("locked_until")
            if locked_until and time.time() >= locked_until:
                entry["locked_until"]    = None
                entry["failed_attempts"] = 0
                cleared += 1
    return cleared
