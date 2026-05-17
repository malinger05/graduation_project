"""
sessions.py  —  Login session storage for the middleware.

"Who is this person and are they still logged in?"

When MIDDLEWARE_DB_URL is set, sessions live in the session_state table and
survive a middleware restart. When unset, sessions fall back to an in-memory
dict (same behaviour as before the middleware DB existed).
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import db
from models import SessionState

_ttl_seconds: int = 900

# In-memory fallback when the middleware DB is disabled.
_memory: dict[str, dict] = {}
_memory_lock = threading.Lock()


def configure(ttl_seconds: int) -> None:
    global _ttl_seconds
    _ttl_seconds = ttl_seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(last_active: datetime) -> bool:
    return last_active < _now() - timedelta(seconds=_ttl_seconds)


def _row_to_dict(row: SessionState) -> dict:
    return {
        "jwt":            row.jwt,
        "account_id":     row.account_id,
        "account_number": row.account_number,
        "balance":        float(row.balance),
        "customer_name":  row.customer_name,
    }


def create(
    *,
    jwt: str,
    account_id: int,
    account_number: str,
    balance: float,
    customer_name: str,
) -> str:
    """Create a session and return the opaque session token."""
    token = secrets.token_hex(32)
    now   = _now()

    if db.is_enabled():
        with db.db_session() as s:
            s.add(SessionState(
                session_token  = token,
                jwt            = jwt,
                account_id     = account_id,
                account_number = account_number,
                balance        = balance,
                customer_name  = customer_name,
                last_active    = now,
                created_at     = now,
            ))
        return token

    with _memory_lock:
        _memory[token] = {
            "jwt":            jwt,
            "account_id":     account_id,
            "account_number": account_number,
            "balance":        balance,
            "customer_name":  customer_name,
            "last_active":    time.time(),
        }
    return token


def get(token: str) -> dict:
    """
    Load a session by token. Refreshes last_active on success.
    Raises HTTPException(401) if missing or expired.
    """
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(SessionState, token)
            if row is None or _is_expired(row.last_active):
                if row is not None:
                    s.delete(row)
                raise HTTPException(401, "Invalid or expired session. Please log in again.")
            row.last_active = _now()
            return _row_to_dict(row)

    with _memory_lock:
        sess = _memory.get(token)
        if not sess:
            raise HTTPException(401, "Invalid or expired session. Please log in again.")
        if time.time() - sess.get("last_active", 0) > _ttl_seconds:
            del _memory[token]
            raise HTTPException(401, "Invalid or expired session. Please log in again.")
        sess["last_active"] = time.time()
        return {
            "jwt":            sess["jwt"],
            "account_id":     sess["account_id"],
            "account_number": sess["account_number"],
            "balance":        sess["balance"],
            "customer_name":  sess["customer_name"],
        }


def update_balance(token: str, balance: float) -> None:
    """Persist a new cached balance after deposit or withdraw."""
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(SessionState, token)
            if row is not None:
                row.balance = balance
        return

    with _memory_lock:
        if token in _memory:
            _memory[token]["balance"] = balance


def touch(token: str) -> bool:
    """
    Refresh last_active without returning session data.
    Returns False if the session is missing or already expired.
    """
    if not token:
        return False

    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(SessionState, token)
            if row is None or _is_expired(row.last_active):
                return False
            row.last_active = _now()
            return True

    with _memory_lock:
        sess = _memory.get(token)
        if not sess:
            return False
        if time.time() - sess.get("last_active", 0) > _ttl_seconds:
            return False
        sess["last_active"] = time.time()
        return True


def remove(token: str) -> None:
    if db.is_enabled():
        with db.db_session() as s:
            row = s.get(SessionState, token)
            if row is not None:
                s.delete(row)
        return

    with _memory_lock:
        _memory.pop(token, None)


def cleanup_expired() -> int:
    """Delete idle sessions. Returns the number removed."""
    cutoff_dt = _now() - timedelta(seconds=_ttl_seconds)
    removed   = 0

    if db.is_enabled():
        from sqlalchemy import delete

        with db.db_session() as s:
            result = s.execute(
                delete(SessionState).where(SessionState.last_active < cutoff_dt)
            )
            removed = result.rowcount or 0
    else:
        cutoff = time.time() - _ttl_seconds
        with _memory_lock:
            stale = [k for k, v in _memory.items() if v.get("last_active", 0) < cutoff]
            for k in stale:
                del _memory[k]
            removed = len(stale)

    return removed
