"""
retention.py  —  Operational retention for middleware Postgres tables.

Deletes rows older than configured thresholds. This is not cold-storage
archival (S3, tape, etc.) — production systems often export to an archive
before delete; here we apply an in-database retention policy only.

Tables affected:
  - transaction_logs      — rows with created_at older than retention window
  - idempotency_records   — rows with expires_at in the past (TTL already set at insert)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

import db
from models import IdempotencyRecord, TransactionLog


def purge_transaction_logs(retention_days: int) -> int:
    """
    Remove audit log rows older than retention_days.
    Returns 0 if retention_days <= 0 or DB is disabled.
    """
    if retention_days <= 0 or not db.is_enabled():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with db.db_session() as s:
        result = s.execute(
            delete(TransactionLog).where(TransactionLog.created_at < cutoff)
        )
        return result.rowcount or 0


def purge_expired_idempotency() -> int:
    """Remove idempotency rows past their expires_at timestamp."""
    if not db.is_enabled():
        return 0

    now = datetime.now(timezone.utc)
    with db.db_session() as s:
        result = s.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now)
        )
        return result.rowcount or 0


def run_retention(transaction_log_retention_days: int) -> dict[str, int]:
    """Run all retention tasks. Returns {table_name: rows_deleted}."""
    txn_logs = purge_transaction_logs(transaction_log_retention_days)
    idem     = purge_expired_idempotency()
    return {
        "transaction_logs":    txn_logs,
        "idempotency_records": idem,
    }
