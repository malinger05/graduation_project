"""Unit tests for canonical.py — payload normalization and hashing."""

from __future__ import annotations

import hashlib
import json

import canonical


class TestCanonicalAmount:
    def test_integer_amount_four_decimal_places(self):
        assert canonical._canonical_amount(100) == "100.0000"

    def test_float_amount_quantized(self):
        assert canonical._canonical_amount(10.5) == "10.5000"

    def test_string_amount_preserved_scale(self):
        assert canonical._canonical_amount("99.99") == "99.9900"

    def test_trailing_zeros_normalized(self):
        assert canonical._canonical_amount("1.1") == "1.1000"


class TestCanonicalTimestamp:
    def test_strips_whitespace(self):
        assert canonical._canonical_timestamp("  2026-01-01T10:00:00  ") == "2026-01-01T10:00:00"

    def test_accepts_datetime_like_string(self):
        assert canonical._canonical_timestamp("2026-05-23T12:00:00") == "2026-05-23T12:00:00"


class TestBuildCanonicalPayload:
    def test_all_fields_stringified(self):
        payload = canonical.build_canonical_payload(
            account_number="ACC001",
            transaction_type="DEPOSIT",
            amount=50,
            balance_after=150.25,
            reference_id="ref-abc",
            created_at="2026-05-23T12:00:00",
        )
        assert payload["account_number"] == "ACC001"
        assert payload["type"] == "DEPOSIT"
        assert payload["amount"] == "50.0000"
        assert payload["balance_after"] == "150.2500"
        assert payload["reference_id"] == "ref-abc"
        assert payload["created_at"] == "2026-05-23T12:00:00"


class TestCanonicalHash:
    def test_deterministic_for_same_payload(self):
        payload = canonical.build_canonical_payload(
            account_number="A1",
            transaction_type="WITHDRAW",
            amount=20,
            balance_after=80,
            reference_id="r1",
            created_at="2026-01-01T00:00:00",
        )
        h1 = canonical.canonical_hash_of(payload)
        h2 = canonical.canonical_hash_of(payload)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_amount_different_hash(self):
        base = dict(
            account_number="A1",
            transaction_type="DEPOSIT",
            balance_after=100,
            reference_id="r1",
            created_at="2026-01-01T00:00:00",
        )
        h1 = canonical.hash_transaction(amount=10, **base)
        h2 = canonical.hash_transaction(amount=20, **base)
        assert h1 != h2

    def test_sort_keys_stable_json(self):
        payload = {"z": 1, "a": 2}
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert canonical.canonical_hash_of(payload) == expected

    def test_hash_transaction_matches_manual_pipeline(self):
        payload = canonical.build_canonical_payload(
            account_number="999",
            transaction_type="WITHDRAW",
            amount="25.50",
            balance_after="74.50",
            reference_id="REF-9",
            created_at="2026-03-15T08:30:00",
        )
        assert canonical.hash_transaction(
            account_number="999",
            transaction_type="WITHDRAW",
            amount="25.50",
            balance_after="74.50",
            reference_id="REF-9",
            created_at="2026-03-15T08:30:00",
        ) == canonical.canonical_hash_of(payload)

    def test_withdraw_and_deposit_same_fields_differ_by_type(self):
        kwargs = dict(
            account_number="A1",
            amount=100,
            balance_after=200,
            reference_id="r",
            created_at="2026-01-01",
        )
        assert canonical.hash_transaction(transaction_type="DEPOSIT", **kwargs) != canonical.hash_transaction(
            transaction_type="WITHDRAW", **kwargs
        )
