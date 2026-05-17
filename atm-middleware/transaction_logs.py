"""
transaction_logs.py  —  Append-only audit trail for middleware HTTP traffic.

"What happened, when, and who did it?"

One row per handled /atm/* request (success, error, or idempotency cache hit).
Rows are append-only at write time; old rows are removed by retention.py
according to TRANSACTION_LOG_RETENTION_DAYS. No-op when the middleware DB
is disabled.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import db
from models import TransactionLog

DEFAULT_CHANNEL = "ATM_WEB"

_SENSITIVE_KEYS = frozenset({
    "pin",
    "password",
    "jwt",
    "token",
    "sessiontoken",
    "eth_private_key",
    "authorization",
    "x-service-token",
    "x-session-token",
    "middleware_service_token",
})


def sanitize(data: Any) -> Any:
    """Remove secrets before persisting request/response bodies."""
    if isinstance(data, dict):
        return {
            k: "***REDACTED***" if str(k).lower() in _SENSITIVE_KEYS else sanitize(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [sanitize(item) for item in data]
    return data


def log_event(
    *,
    endpoint: str,
    http_method: str,
    outcome: str,
    response_status_code: int,
    account_number: str | None = None,
    channel: str = DEFAULT_CHANNEL,
    idempotency_key: str | None = None,
    request_body: dict | None = None,
    response_body: Any = None,
    duration_ms: int | None = None,
    error_message: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """
    Append one audit row. outcome is one of: success, error, cached.
    """
    if not db.is_enabled():
        return

    safe_request  = sanitize(request_body) if request_body is not None else None
    safe_response = sanitize(response_body) if response_body is not None else None

    with db.db_session() as s:
        s.add(TransactionLog(
            log_id               = uuid.uuid4().hex,
            correlation_id       = correlation_id,
            created_at           = datetime.now(timezone.utc),
            account_number       = account_number,
            channel              = channel or DEFAULT_CHANNEL,
            http_method          = http_method,
            endpoint             = endpoint,
            idempotency_key      = idempotency_key,
            request_body         = safe_request,
            response_status_code = response_status_code,
            response_body        = safe_response,
            outcome              = outcome,
            duration_ms          = duration_ms,
            error_message        = error_message,
        ))
