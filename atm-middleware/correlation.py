"""
correlation.py  —  Step-by-step trace of one business operation across systems.

"What happened to this transaction across middleware, Core Banking, and blockchain?"

Each logical request gets a correlation_id. Every hop appends one row so engineers
can search a single ID and see the full path. No-op when the middleware DB is disabled.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import db
from models import CorrelationLog
from transaction_logs import sanitize


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def log_step(
    correlation_id: str,
    step: str,
    system: str,
    status: str,
    *,
    account_number: str | None = None,
    endpoint: str | None = None,
    message: str | None = None,
    detail: Any = None,
) -> None:
    """
    Append one step for a correlation trace.

    status: ok | error | cached | skipped
    system: middleware | core_banking | sepolia
    """
    if not correlation_id or not db.is_enabled():
        return

    safe_detail = sanitize(detail) if detail is not None else None

    with db.db_session() as s:
        s.add(CorrelationLog(
            log_id          = uuid.uuid4().hex,
            correlation_id  = correlation_id,
            step            = step,
            system          = system,
            status          = status,
            message         = message,
            detail          = safe_detail,
            account_number  = account_number,
            endpoint        = endpoint,
            created_at      = datetime.now(timezone.utc),
        ))
