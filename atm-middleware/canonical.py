"""
Canonical payload + hash for blockchain integrity logging.

Used by BOTH:
  - middleware.py inline flow (after a successful deposit/withdraw)
  - blockchain_worker.py reconciliation (recomputed from Core Banking row)

If the two ever disagree for the same transaction, that's a tamper signal.

The payload uses ONLY fields durably stored in Core Banking — no in-memory
state — so the hash is fully reproducible from the row.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal


def _canonical_amount(x) -> str:
    """4-dp string form, matching Spring Boot's BigDecimal(precision=19, scale=4)."""
    return str(Decimal(str(x)).quantize(Decimal("0.0001")))


def _canonical_timestamp(x) -> str:
    """Trim Spring Boot's LocalDateTime to a stable string."""
    return str(x).strip()


def build_canonical_payload(
    *,
    account_number: str,
    transaction_type: str,
    amount,
    balance_after,
    reference_id: str,
    created_at,
) -> dict:
    return {
        "account_number":  str(account_number),
        "type":            str(transaction_type),
        "amount":          _canonical_amount(amount),
        "balance_after":   _canonical_amount(balance_after),
        "reference_id":    str(reference_id),
        "created_at":      _canonical_timestamp(created_at),
    }


def canonical_hash_of(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def hash_transaction(
    *,
    account_number: str,
    transaction_type: str,
    amount,
    balance_after,
    reference_id: str,
    created_at,
) -> str:
    payload = build_canonical_payload(
        account_number=account_number,
        transaction_type=transaction_type,
        amount=amount,
        balance_after=balance_after,
        reference_id=reference_id,
        created_at=created_at,
    )
    return canonical_hash_of(payload)
