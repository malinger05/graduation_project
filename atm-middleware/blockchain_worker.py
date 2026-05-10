"""
blockchain_worker.py  —  Layer 2 reconciliation worker

Three background tasks, each in its own daemon thread, all going through
Spring Boot HTTP (admin_client) — never directly to PostgreSQL.

  1. submit-retry   : transactions stuck in PENDING_SUBMIT / FAILED_SUBMIT —
                      recompute their canonical hash from the durable row,
                      submit to Sepolia, PATCH /blockchain.
  2. confirm-poll   : transactions in SUBMITTED — poll Sepolia for the
                      receipt, PATCH /confirm once mined successfully.
  3. tamper-check   : CONFIRMED transactions — recompute the canonical hash
                      from the row and compare to the stored hash; PATCH
                      /tampered on mismatch.

This module owns NO state. All durable data lives in Core Banking's database;
all chain interaction goes through the Web3 contract handle owned by
middleware.py.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from admin_client import AdminClient
from canonical import hash_transaction


# ── Tunables ──────────────────────────────────────────────────────────────────

RETRY_INTERVAL_SECONDS   = float(os.environ.get("WORKER_RETRY_INTERVAL_SECONDS",   "30"))
CONFIRM_INTERVAL_SECONDS = float(os.environ.get("WORKER_CONFIRM_INTERVAL_SECONDS", "60"))
TAMPER_INTERVAL_SECONDS  = float(os.environ.get("WORKER_TAMPER_INTERVAL_SECONDS",  "300"))

RETRY_BATCH_SIZE   = int(os.environ.get("WORKER_RETRY_BATCH_SIZE",   "25"))
CONFIRM_BATCH_SIZE = int(os.environ.get("WORKER_CONFIRM_BATCH_SIZE", "25"))
TAMPER_BATCH_SIZE  = int(os.environ.get("WORKER_TAMPER_BATCH_SIZE",  "100"))

TAMPER_LOOKBACK_HOURS = int(os.environ.get("WORKER_TAMPER_LOOKBACK_HOURS", "24"))

MAX_SUBMIT_ATTEMPTS = int(os.environ.get("WORKER_MAX_SUBMIT_ATTEMPTS", "8"))


# ── Per-row helpers ───────────────────────────────────────────────────────────

def _row_canonical_hash(row: dict) -> str:
    return hash_transaction(
        account_number=row.get("accountNumber") or "",
        transaction_type=row.get("transactionType") or "",
        amount=row.get("amount", 0),
        balance_after=row.get("balanceAfter", 0),
        reference_id=row.get("referenceId") or "",
        created_at=row.get("createdAt") or "",
    )


# ── Job 1: submit-retry ───────────────────────────────────────────────────────

def run_submit_retry_once(admin: AdminClient, submit_to_chain: Callable[[str], str | None]) -> int:
    """One sweep of the submit-retry job. Returns rows processed."""
    rows = admin.get_pending_submit(limit=RETRY_BATCH_SIZE, max_attempts=MAX_SUBMIT_ATTEMPTS)
    for row in rows:
        tx_id = row["transactionId"]
        try:
            canonical = _row_canonical_hash(row)
            chain_tx  = submit_to_chain(canonical)
            admin.patch_blockchain(
                transaction_id=tx_id,
                canonical_hash=canonical,
                blockchain_tx=chain_tx,
                submit_error=None if chain_tx else "blockchain submit returned no tx hash",
            )
            print(f"[Worker:retry] tx {tx_id} → {'submitted ' + chain_tx if chain_tx else 'still pending'}")
        except Exception as e:
            try:
                admin.patch_blockchain(
                    transaction_id=tx_id,
                    canonical_hash=None,
                    blockchain_tx=None,
                    submit_error=str(e)[:900],
                )
            except Exception as patch_err:
                print(f"[Worker:retry] tx {tx_id} patch-after-failure failed: {patch_err}")
            print(f"[Worker:retry] tx {tx_id} submit failed: {e}")
    return len(rows)


# ── Job 2: confirm-poll ───────────────────────────────────────────────────────

def run_confirm_poll_once(admin: AdminClient, get_receipt: Callable[[str], dict | None]) -> int:
    """One sweep of the confirm-poll job. Returns rows confirmed."""
    rows = admin.get_submitted(limit=CONFIRM_BATCH_SIZE)
    confirmed = 0
    for row in rows:
        tx_id = row["transactionId"]
        chain_tx = row.get("blockchainTx")
        if not chain_tx:
            continue
        try:
            receipt = get_receipt(chain_tx)
        except Exception as e:
            print(f"[Worker:confirm] tx {tx_id} receipt lookup failed: {e}")
            continue
        if receipt is None:
            continue  # not yet mined
        # Web3 receipts have status==1 on success; the helper passed in returns
        # a dict with a "status" key normalized to int.
        if receipt.get("status") == 1:
            try:
                admin.patch_confirm(tx_id)
                confirmed += 1
                print(f"[Worker:confirm] tx {tx_id} confirmed on chain ({chain_tx})")
            except Exception as e:
                print(f"[Worker:confirm] tx {tx_id} patch /confirm failed: {e}")
        else:
            try:
                admin.patch_blockchain(
                    transaction_id=tx_id,
                    canonical_hash=None,
                    blockchain_tx=None,
                    submit_error=f"chain receipt status={receipt.get('status')}",
                )
                print(f"[Worker:confirm] tx {tx_id} chain-failed, requeued for retry")
            except Exception as e:
                print(f"[Worker:confirm] tx {tx_id} patch /blockchain failed: {e}")
    return confirmed


# ── Job 3: tamper-check ───────────────────────────────────────────────────────

def run_tamper_check_once(admin: AdminClient,
                          verify_on_chain: Callable[[str], bool] | None = None) -> int:
    """One sweep of the tamper-check job. Returns rows flagged tampered."""
    since = datetime.now(timezone.utc) - timedelta(hours=TAMPER_LOOKBACK_HOURS)
    rows = admin.get_for_tamper_check(
        since_iso=since.isoformat(timespec="seconds").replace("+00:00", ""),
        limit=TAMPER_BATCH_SIZE,
    )
    flagged = 0
    for row in rows:
        tx_id = row["transactionId"]
        stored = row.get("canonicalHash")
        recomputed = _row_canonical_hash(row)

        if stored and stored != recomputed:
            reason = f"DB row mutated: stored={stored} recomputed={recomputed}"
            try:
                admin.patch_tampered(tx_id, reason)
                flagged += 1
                print(f"[Worker:tamper] tx {tx_id} TAMPERED — {reason}")
            except Exception as e:
                print(f"[Worker:tamper] tx {tx_id} patch /tampered failed: {e}")
            continue

        # Optional second check: hash exists on chain.
        if verify_on_chain and stored:
            try:
                if not verify_on_chain(stored):
                    reason = f"chain verify failed: hash {stored} not on contract"
                    admin.patch_tampered(tx_id, reason)
                    flagged += 1
                    print(f"[Worker:tamper] tx {tx_id} TAMPERED — {reason}")
            except Exception as e:
                print(f"[Worker:tamper] tx {tx_id} chain verify error: {e}")
    return flagged


# ── Daemon thread loops ───────────────────────────────────────────────────────

def _loop(name: str, interval: float, body: Callable[[], None]) -> None:
    print(f"[Worker:{name}] loop started, interval={interval:.0f}s")
    while True:
        try:
            body()
        except Exception as e:
            print(f"[Worker:{name}] iteration failed: {e}")
        time.sleep(interval)


def start(admin: AdminClient,
          submit_to_chain: Callable[[str], str | None],
          get_receipt: Callable[[str], dict | None],
          verify_on_chain: Callable[[str], bool] | None = None) -> None:
    """
    Spawn the three reconciliation daemons. Call once at middleware startup.
    All callbacks are provided by middleware.py so this module stays free of
    Web3 / RPC concerns.
    """
    threading.Thread(
        target=_loop,
        args=("retry", RETRY_INTERVAL_SECONDS,
              lambda: run_submit_retry_once(admin, submit_to_chain)),
        daemon=True,
        name="bc-worker-retry",
    ).start()
    threading.Thread(
        target=_loop,
        args=("confirm", CONFIRM_INTERVAL_SECONDS,
              lambda: run_confirm_poll_once(admin, get_receipt)),
        daemon=True,
        name="bc-worker-confirm",
    ).start()
    threading.Thread(
        target=_loop,
        args=("tamper", TAMPER_INTERVAL_SECONDS,
              lambda: run_tamper_check_once(admin, verify_on_chain)),
        daemon=True,
        name="bc-worker-tamper",
    ).start()
