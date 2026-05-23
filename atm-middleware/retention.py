"""
retention.py  —  Operational retention for middleware Postgres tables.

Deletes rows older than configured thresholds. This is not cold-storage
archival (S3, tape, etc.) — production systems often export to an archive
before delete; here we apply an in-database retention policy only.

Tables affected:
  - transaction_logs      — rows with created_at older than retention window
  - correlation_logs      — same retention window as transaction_logs
  - idempotency_records   — rows with expires_at in the past (TTL already set at insert)
  - login_lockouts        — permanently locked accounts whose PIN was never reset,
                            after PERMANENTLY_LOCKED_ACCOUNT_CLEANUP_DAYS (default: 60).
                            When such a row is found the account is soft-deleted via
                            Core Banking DELETE /customers/{id}/accounts/{id}.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from sqlalchemy import delete, select

import db
from models import CorrelationLog, IdempotencyRecord, LoginLockout, TransactionLog

# ── Tunables ──────────────────────────────────────────────────────────────────

PERMANENTLY_LOCKED_CLEANUP_DAYS: int = int(
    os.environ.get("PERMANENTLY_LOCKED_ACCOUNT_CLEANUP_DAYS", "60")
)


# ── Standard retention tasks ──────────────────────────────────────────────────

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


def purge_correlation_logs(retention_days: int) -> int:
    """Remove correlation trace rows older than retention_days."""
    if retention_days <= 0 or not db.is_enabled():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with db.db_session() as s:
        result = s.execute(
            delete(CorrelationLog).where(CorrelationLog.created_at < cutoff)
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


# ── Permanently-locked account cleanup ───────────────────────────────────────

def _close_account_via_core_banking(
    account_number: str,
    core_banking_url: str,
    admin_jwt: Optional[str],
) -> bool:
    """
    Resolve the account in Core Banking and soft-delete it.

    Flow:
      1. GET /customers  — find the account number across all customers
         (Core Banking has no direct GET /accounts/{number} endpoint, so we search
         the customer list; this is admin-only and the dataset is small.)
      2. For each customer's accounts, look for the matching accountNumber.
      3. DELETE /customers/{customerId}/accounts/{accountId}

    Returns True on success, False on any error (logs the reason).
    """
    headers = {"Content-Type": "application/json"}
    if admin_jwt:
        headers["Authorization"] = f"Bearer {admin_jwt}"

    try:
        # Resolve account
        resp = requests.get(
            f"{core_banking_url}/customers",
            headers=headers,
            timeout=(3, 15),
        )
        if not resp.ok:
            print(f"[Retention] permanently-locked cleanup: GET /customers failed ({resp.status_code})")
            return False

        customers = resp.json()
        for customer in customers:
            cid = customer.get("customerId")
            if not cid:
                continue
            acc_resp = requests.get(
                f"{core_banking_url}/customers/{cid}/accounts",
                headers=headers,
                timeout=(3, 15),
            )
            if not acc_resp.ok:
                continue
            for acc in acc_resp.json():
                if acc.get("accountNumber") == account_number:
                    aid = acc.get("accountId")
                    if not aid:
                        return False
                    del_resp = requests.delete(
                        f"{core_banking_url}/customers/{cid}/accounts/{aid}",
                        headers=headers,
                        timeout=(3, 15),
                    )
                    if del_resp.ok or del_resp.status_code == 204:
                        print(
                            f"[Retention] permanently-locked cleanup: "
                            f"account {account_number} (id={aid}) closed via Core Banking"
                        )
                        return True
                    else:
                        print(
                            f"[Retention] permanently-locked cleanup: "
                            f"DELETE /customers/{cid}/accounts/{aid} failed "
                            f"({del_resp.status_code}): {del_resp.text[:200]}"
                        )
                        return False
    except Exception as e:
        print(f"[Retention] permanently-locked cleanup: Core Banking call failed: {e}")
        return False

    print(f"[Retention] permanently-locked cleanup: account {account_number} not found in Core Banking")
    return False


def purge_permanently_locked_accounts(
    cleanup_days: int = PERMANENTLY_LOCKED_CLEANUP_DAYS,
    core_banking_url: Optional[str] = None,
    admin_jwt: Optional[str] = None,
) -> int:
    """
    Find login_lockouts rows that are:
      - permanently_locked = TRUE
      - must_reset_pin = TRUE  (admin did NOT unlock — admin unlock sets must_reset_pin)

    Wait: permanently_locked=TRUE means the customer was never unlocked.
    If admin HAD unlocked, permanently_locked=FALSE and must_reset_pin=TRUE.
    So we target: permanently_locked=TRUE AND updated_at < (now - cleanup_days).

    For each such row:
      1. Attempt to soft-delete the account via Core Banking.
      2. On success, delete the lockout row from middleware DB.

    Returns the number of accounts closed.
    """
    if not db.is_enabled() or cleanup_days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=cleanup_days)
    closed = 0

    with db.db_session() as s:
        stale_rows = list(
            s.scalars(
                select(LoginLockout).where(
                    LoginLockout.permanently_locked.is_(True),
                    LoginLockout.must_reset_pin.is_(False),  # admin never unlocked
                    LoginLockout.updated_at < cutoff,
                )
            ).all()
        )

    for row in stale_rows:
        account_number = row.account_number
        print(
            f"[Retention] permanently-locked cleanup: "
            f"account {account_number} locked for >{cleanup_days} days without admin unlock — closing"
        )

        if core_banking_url:
            ok = _close_account_via_core_banking(account_number, core_banking_url, admin_jwt)
        else:
            ok = True  # no Core Banking URL configured — just clean up lockout row

        if ok:
            with db.db_session() as s:
                row_to_del = s.get(LoginLockout, account_number)
                if row_to_del is not None:
                    s.delete(row_to_del)
            closed += 1

    return closed


# ── Main entry point ──────────────────────────────────────────────────────────

def run_retention(
    transaction_log_retention_days: int,
    core_banking_url: Optional[str] = None,
    admin_jwt: Optional[str] = None,
    permanently_locked_cleanup_days: int = PERMANENTLY_LOCKED_CLEANUP_DAYS,
) -> dict[str, int]:
    """Run all retention tasks. Returns {table_name: rows_deleted}."""
    txn_logs = purge_transaction_logs(transaction_log_retention_days)
    corr     = purge_correlation_logs(transaction_log_retention_days)
    idem     = purge_expired_idempotency()
    locked   = purge_permanently_locked_accounts(
        cleanup_days=permanently_locked_cleanup_days,
        core_banking_url=core_banking_url,
        admin_jwt=admin_jwt,
    )
    return {
        "transaction_logs":             txn_logs,
        "correlation_logs":             corr,
        "idempotency_records":          idem,
        "permanently_locked_accounts":  locked,
    }