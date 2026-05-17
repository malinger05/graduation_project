"""
idempotency.py  —  Helpers used by /atm/deposit and /atm/withdraw to make
state-mutating requests safe to retry.

Usage in an endpoint:

    cached = idempotency.begin(key, account_number, endpoint, request_dict)
    if cached is not None:
        return cached            # client retry — return the original response

    response = ...do real work, call Core Banking, etc...

    idempotency.finish(key, account_number, response)
    return response

If MIDDLEWARE_DB_URL is unset, both functions become no-ops and the endpoint
behaves exactly as it did before the middleware DB was added. This lets us
roll out persistence table-by-table without breaking existing clients.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import db
from models import IdempotencyRecord

# How long we remember a key. After this, the same key may be reused for a
# brand-new request.
IDEMPOTENCY_TTL = timedelta(hours=24)

# If a row is stuck in 'in_progress' for longer than this, we assume the
# previous attempt crashed mid-flight and let the new attempt overwrite it.
# Keep this just slightly longer than the slowest Core Banking call.
IN_PROGRESS_STALE_AFTER = timedelta(seconds=60)


def _fingerprint(endpoint: str, account_number: str, request_body: dict) -> str:
    """Stable SHA-256 of the request — used to detect key reuse with a different body."""
    payload = json.dumps(
        {"endpoint": endpoint, "account": account_number, "body": request_body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def begin(
    idempotency_key: str | None,
    account_number: str,
    endpoint: str,
    request_body: dict,
) -> dict | None:
    """
    Look up (or claim) an idempotency record for this request.

    Returns:
        - dict  : the cached response body — caller MUST return this without
                  doing any further work.
        - None  : this is a fresh request. Caller proceeds normally and must
                  call `finish()` once the response is ready.

    Raises:
        - HTTPException(422) : same key was used before with a different body.
        - HTTPException(409) : a previous attempt with this key is still
                               in-flight (and not yet stale).

    No-ops (returns None) when:
        - idempotency_key is None        (client opted out)
        - the middleware DB is disabled  (MIDDLEWARE_DB_URL unset)
    """
    if not idempotency_key or not db.is_enabled():
        return None

    fp  = _fingerprint(endpoint, account_number, request_body)
    now = datetime.now(timezone.utc)

    with db.db_session() as s:
        rec = s.get(IdempotencyRecord, (account_number, idempotency_key))

        if rec is not None:
            # Expired? Drop it and treat as fresh.
            if rec.expires_at <= now:
                s.delete(rec)
                s.flush()
                rec = None
            # Stale in-progress? Previous attempt almost certainly crashed.
            elif rec.status == "in_progress" and (now - rec.created_at) > IN_PROGRESS_STALE_AFTER:
                s.delete(rec)
                s.flush()
                rec = None

        if rec is not None:
            if rec.request_fingerprint != fp:
                raise HTTPException(
                    422,
                    "Idempotency-Key was already used with a different request body.",
                )
            if rec.status == "completed":
                return rec.response_body
            # status == 'in_progress' and still fresh
            raise HTTPException(
                409,
                "A request with this Idempotency-Key is still being processed. Please retry shortly.",
            )

        # Fresh request — record the in-progress marker.
        s.add(IdempotencyRecord(
            account_number      = account_number,
            idempotency_key     = idempotency_key,
            endpoint            = endpoint,
            request_fingerprint = fp,
            status              = "in_progress",
            created_at          = now,
            expires_at          = now + IDEMPOTENCY_TTL,
        ))
    return None


def finish(
    idempotency_key: str | None,
    account_number: str,
    response_body: dict,
    response_status_code: int = 200,
) -> None:
    """
    Mark the idempotency record as completed and cache the response so future
    retries with the same key return the same answer.

    Safe to call when idempotency_key is None or the DB is disabled — both
    cases are no-ops.
    """
    if not idempotency_key or not db.is_enabled():
        return

    with db.db_session() as s:
        rec = s.get(IdempotencyRecord, (account_number, idempotency_key))
        if rec is None:
            return  # nothing to update — begin() was never called or row was evicted
        rec.status               = "completed"
        rec.response_status_code = response_status_code
        rec.response_body        = response_body
        rec.completed_at         = datetime.now(timezone.utc)
