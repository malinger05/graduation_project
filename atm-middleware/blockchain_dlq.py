"""
blockchain_dlq.py — Dead-letter tracking for Core Banking FAILED_SUBMIT rows.

When the reconciliation worker detects a failed blockchain submission that
requires manual review, we record it here and emit a one-time alert per
transaction_id (survives middleware restarts when MIDDLEWARE_DB_URL is set).
"""

from __future__ import annotations

from datetime import datetime, timezone

import db
from models import BlockchainDlq


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_and_should_alert(
    transaction_id: int,
    *,
    account_number: str | None,
    last_error: str | None,
) -> bool:
    """
    Insert or refresh DLQ row. Returns True if an alert should be sent now
    (first time seen, or error text changed since last notification).
    """
    if not db.is_enabled():
        return True  # still log/print from worker when DB is off

    now = _now()
    err = (last_error or "")[:1000]

    with db.db_session() as s:
        row = s.get(BlockchainDlq, transaction_id)
        if row is None:
            s.add(
                BlockchainDlq(
                    transaction_id=transaction_id,
                    account_number=account_number,
                    last_error=err,
                    failed_at=now,
                    updated_at=now,
                    notified_at=None,
                )
            )
            return True

        prev_err = row.last_error
        row.account_number = account_number or row.account_number
        row.last_error = err
        row.updated_at = now
        if row.notified_at is None:
            return True
        if prev_err != err:
            row.notified_at = None
            return True
        return False


def mark_notified(transaction_id: int) -> None:
    if not db.is_enabled():
        return
    now = _now()
    with db.db_session() as s:
        row = s.get(BlockchainDlq, transaction_id)
        if row is None:
            return
        row.notified_at = now
        row.updated_at = now
