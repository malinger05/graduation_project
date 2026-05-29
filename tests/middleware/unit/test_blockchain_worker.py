"""Unit tests for blockchain_worker.py — reconciliation single-pass jobs."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import blockchain_worker
from canonical import hash_transaction


def _sample_row(**overrides) -> dict:
    base = {
        "transactionId": 101,
        "accountNumber": "ACC001",
        "transactionType": "DEPOSIT",
        "amount": 100,
        "balanceAfter": 500,
        "referenceId": "REF-1",
        "createdAt": "2026-05-23T12:00:00",
        "canonicalHash": None,
        "blockchainTx": None,
    }
    base.update(overrides)
    return base


class TestRowCanonicalHash:
    def test_row_canonical_hash_matches_hash_transaction(self):
        row = _sample_row()
        assert blockchain_worker._row_canonical_hash(row) == hash_transaction(
            account_number="ACC001",
            transaction_type="DEPOSIT",
            amount=100,
            balance_after=500,
            reference_id="REF-1",
            created_at="2026-05-23T12:00:00",
        )

    def test_row_canonical_hash_handles_missing_fields(self):
        h = blockchain_worker._row_canonical_hash({})
        assert isinstance(h, str)
        assert len(h) == 64


class TestSubmitRetry:
    def test_empty_batch_returns_zero(self):
        admin = MagicMock()
        admin.get_pending_submit.return_value = []
        assert blockchain_worker.run_submit_retry_once(admin, lambda h: "0xtx") == 0

    def test_successful_submit_patches_blockchain(self):
        admin = MagicMock()
        row = _sample_row()
        admin.get_pending_submit.return_value = [row]
        n = blockchain_worker.run_submit_retry_once(admin, lambda h: "0xabc")
        assert n == 1
        admin.patch_blockchain.assert_called_once()
        call = admin.patch_blockchain.call_args.kwargs
        assert call["transaction_id"] == 101
        assert call["blockchain_tx"] == "0xabc"
        assert call["canonical_hash"] is not None

    def test_submit_returns_none_still_patches(self):
        admin = MagicMock()
        admin.get_pending_submit.return_value = [_sample_row()]
        blockchain_worker.run_submit_retry_once(admin, lambda h: None)
        call = admin.patch_blockchain.call_args.kwargs
        assert call["blockchain_tx"] is None
        assert "submit returned no tx" in (call["submit_error"] or "")

    def test_submit_exception_patches_error(self):
        admin = MagicMock()
        admin.get_pending_submit.return_value = [_sample_row()]

        def boom(_):
            raise RuntimeError("RPC down")

        blockchain_worker.run_submit_retry_once(admin, boom)
        assert admin.patch_blockchain.call_count >= 1
        err_call = admin.patch_blockchain.call_args.kwargs
        assert "RPC down" in (err_call.get("submit_error") or "")

    def test_multiple_rows_processed(self):
        admin = MagicMock()
        admin.get_pending_submit.return_value = [
            _sample_row(transactionId=1),
            _sample_row(transactionId=2),
        ]
        assert blockchain_worker.run_submit_retry_once(admin, lambda h: "0x1") == 2


class TestConfirmPoll:
    def test_empty_submitted_returns_zero(self):
        admin = MagicMock()
        admin.get_submitted.return_value = []
        assert blockchain_worker.run_confirm_poll_once(admin, lambda t: None) == 0

    def test_skips_row_without_blockchain_tx(self):
        admin = MagicMock()
        admin.get_submitted.return_value = [_sample_row(blockchainTx=None)]
        assert blockchain_worker.run_confirm_poll_once(admin, lambda t: {"status": 1}) == 0
        admin.patch_confirm.assert_not_called()

    def test_pending_receipt_not_confirmed(self):
        admin = MagicMock()
        admin.get_submitted.return_value = [_sample_row(blockchainTx="0x1")]
        assert blockchain_worker.run_confirm_poll_once(admin, lambda t: None) == 0

    def test_success_receipt_calls_patch_confirm(self):
        admin = MagicMock()
        admin.get_submitted.return_value = [_sample_row(blockchainTx="0xok")]
        n = blockchain_worker.run_confirm_poll_once(admin, lambda t: {"status": 1})
        assert n == 1
        admin.patch_confirm.assert_called_once_with(101)

    def test_failed_receipt_requeues(self):
        admin = MagicMock()
        admin.get_submitted.return_value = [_sample_row(blockchainTx="0xfail")]
        blockchain_worker.run_confirm_poll_once(admin, lambda t: {"status": 0})
        admin.patch_blockchain.assert_called()

    def test_receipt_lookup_exception_continues(self):
        admin = MagicMock()
        admin.get_submitted.return_value = [_sample_row(blockchainTx="0xe")]

        def fail(_):
            raise ConnectionError("network")

        assert blockchain_worker.run_confirm_poll_once(admin, fail) == 0

    def test_patch_confirm_failure_does_not_increment_count(self):
        admin = MagicMock()
        admin.get_submitted.return_value = [_sample_row(blockchainTx="0x1")]
        admin.patch_confirm.side_effect = RuntimeError("CB down")
        assert blockchain_worker.run_confirm_poll_once(admin, lambda t: {"status": 1}) == 0


class TestTamperCheck:
    def test_no_rows_returns_zero(self):
        admin = MagicMock()
        admin.get_for_tamper_check.return_value = []
        assert blockchain_worker.run_tamper_check_once(admin) == 0

    def test_hash_mismatch_flags_tampered(self):
        admin = MagicMock()
        row = _sample_row(canonicalHash="deadbeef" * 8)
        admin.get_for_tamper_check.return_value = [row]
        n = blockchain_worker.run_tamper_check_once(admin)
        assert n == 1
        admin.patch_tampered.assert_called_once()
        reason = admin.patch_tampered.call_args[0][1]
        assert "DB row mutated" in reason

    def test_matching_hash_not_flagged(self):
        admin = MagicMock()
        row = _sample_row()
        row["canonicalHash"] = blockchain_worker._row_canonical_hash(row)
        admin.get_for_tamper_check.return_value = [row]
        assert blockchain_worker.run_tamper_check_once(admin) == 0
        admin.patch_tampered.assert_not_called()

    def test_chain_verify_failure_does_not_flag_when_db_hash_matches(self):
        admin = MagicMock()
        row = _sample_row()
        stored = blockchain_worker._row_canonical_hash(row)
        row["canonicalHash"] = stored
        admin.get_for_tamper_check.return_value = [row]
        n = blockchain_worker.run_tamper_check_once(admin, verify_on_chain=lambda h: False)
        assert n == 0
        admin.patch_tampered.assert_not_called()

    def test_chain_verify_success_no_flag(self):
        admin = MagicMock()
        row = _sample_row()
        row["canonicalHash"] = blockchain_worker._row_canonical_hash(row)
        admin.get_for_tamper_check.return_value = [row]
        n = blockchain_worker.run_tamper_check_once(admin, verify_on_chain=lambda h: True)
        assert n == 0

    def test_verify_exception_does_not_crash(self):
        admin = MagicMock()
        row = _sample_row()
        row["canonicalHash"] = blockchain_worker._row_canonical_hash(row)
        admin.get_for_tamper_check.return_value = [row]

        def boom(_):
            raise ValueError("contract error")

        assert blockchain_worker.run_tamper_check_once(admin, verify_on_chain=boom) == 0

    def test_empty_stored_hash_skips_mismatch_branch(self):
        admin = MagicMock()
        row = _sample_row(canonicalHash=None)
        admin.get_for_tamper_check.return_value = [row]
        assert blockchain_worker.run_tamper_check_once(admin) == 0

    def test_tamper_patch_failure_logged(self):
        admin = MagicMock()
        row = _sample_row(canonicalHash="deadbeef" * 8)
        admin.get_for_tamper_check.return_value = [row]
        admin.patch_tampered.side_effect = RuntimeError("CB error")
        assert blockchain_worker.run_tamper_check_once(admin) == 0

    def test_confirm_requeue_patch_failure(self):
        admin = MagicMock()
        admin.get_submitted.return_value = [_sample_row(blockchainTx="0xfail")]
        admin.patch_blockchain.side_effect = RuntimeError("patch failed")
        assert blockchain_worker.run_confirm_poll_once(admin, lambda t: {"status": 0}) == 0

    def test_submit_patch_after_failure(self):
        admin = MagicMock()
        admin.get_pending_submit.return_value = [_sample_row()]

        def boom(_):
            raise RuntimeError("RPC down")

        admin.patch_blockchain.side_effect = [RuntimeError("patch failed"), None]
        assert blockchain_worker.run_submit_retry_once(admin, boom) == 1


class TestWorkerStart:
    def test_start_spawns_threads(self, monkeypatch):
        started = []

        def fake_thread(target=None, args=(), daemon=False, name=None):
            started.append(name)
            return MagicMock()

        monkeypatch.setattr(blockchain_worker.threading, "Thread", fake_thread)
        blockchain_worker.start(
            get_admin=lambda: MagicMock(),
            submit_to_chain=lambda h: "0x",
            get_receipt=lambda t: {"status": 1},
        )
        assert "bc-worker-retry" in started
        assert "bc-worker-confirm" in started
        assert "bc-worker-tamper" in started
