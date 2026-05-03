import unittest

from atm_architecture import ATMApp, Indexer


class FakeAccountsRepo:
    def __init__(self):
        self.table_name = "accounts"


class FakeTransactionsRepo:
    def __init__(self):
        self.created_response = None
        self.submitted = []
        self.retries = []
        self.by_id = {}
        self.submission_rows = []

    def create_local_transaction_atomic(self, _table, _account, _tx_type, _amount, _created_at):
        return self.created_response

    def mark_transaction_submitted(self, tx_id, tx_hash):
        self.submitted.append((tx_id, tx_hash))

    def schedule_submission_retry(self, tx_id, reason, delay_seconds=10):
        self.retries.append((tx_id, reason, delay_seconds))

    def get_transaction_by_id(self, tx_id):
        return self.by_id.get(tx_id, {"status": "confirmed"})

    def get_transactions_for_submission(self, limit=25, max_retries=8):
        _ = (limit, max_retries)
        return self.submission_rows


class FakeBlockchain:
    def __init__(self, tx_hash="0xabc", fail=False):
        self.tx_hash = tx_hash
        self.fail = fail
        self.submitted_hashes = []

    def submit_log_hash(self, canonical_hash):
        self.submitted_hashes.append(canonical_hash)
        if self.fail:
            raise RuntimeError("rpc unavailable")
        return self.tx_hash


class FakeIndexer:
    def sync_once(self):
        return 1


class TransactionFlowTests(unittest.TestCase):
    def _make_app(self, tx_repo, blockchain):
        app = ATMApp(FakeAccountsRepo(), tx_repo, blockchain, FakeIndexer())
        app.current_account = "1001"
        return app

    def test_record_success_submits_chain_and_marks_submitted(self):
        tx_repo = FakeTransactionsRepo()
        tx_repo.created_response = (
            {
                "id": 42,
                "new_balance": 525.0,
                "canonical_hash": "hash1",
                "status": "initiated",
            },
            "",
        )
        blockchain = FakeBlockchain(tx_hash="0x123")
        app = self._make_app(tx_repo, blockchain)

        ok, msg, on_chain = app.deposit(25.0)

        self.assertTrue(ok)
        self.assertTrue(on_chain)
        self.assertIn("New balance: $525.00", msg)
        self.assertEqual(tx_repo.submitted, [(42, "0x123")])
        self.assertEqual(tx_repo.retries, [])

    def test_record_chain_failure_schedules_retry_after_local_commit(self):
        tx_repo = FakeTransactionsRepo()
        tx_repo.created_response = (
            {
                "id": 43,
                "new_balance": 475.0,
                "canonical_hash": "hash2",
                "status": "initiated",
            },
            "",
        )
        blockchain = FakeBlockchain(fail=True)
        app = self._make_app(tx_repo, blockchain)

        ok, msg, on_chain = app.withdraw(25.0)

        self.assertTrue(ok)
        self.assertFalse(on_chain)
        self.assertIn("Recorded locally; blockchain sync will retry shortly.", msg)
        self.assertEqual(tx_repo.submitted, [])
        self.assertEqual(len(tx_repo.retries), 1)
        self.assertEqual(tx_repo.retries[0][0], 43)

    def test_record_insufficient_funds_returns_failure(self):
        tx_repo = FakeTransactionsRepo()
        tx_repo.created_response = (None, "Insufficient funds")
        blockchain = FakeBlockchain()
        app = self._make_app(tx_repo, blockchain)

        ok, msg, on_chain = app.withdraw(5000.0)

        self.assertFalse(ok)
        self.assertFalse(on_chain)
        self.assertEqual(msg, "Insufficient funds")
        self.assertEqual(tx_repo.submitted, [])
        self.assertEqual(tx_repo.retries, [])

    def test_indexer_retry_submits_initiated_rows(self):
        tx_repo = FakeTransactionsRepo()
        tx_repo.submission_rows = [
            {"id": 1, "canonical_hash": "h1", "retry_count": 0},
            {"id": 2, "canonical_hash": "h2", "retry_count": 1},
        ]
        blockchain = FakeBlockchain(tx_hash="0x999")
        indexer = Indexer(tx_repo, blockchain)

        submitted_count = indexer.process_submission_retries_once()

        self.assertEqual(submitted_count, 2)
        self.assertEqual(tx_repo.submitted, [(1, "0x999"), (2, "0x999")])


if __name__ == "__main__":
    unittest.main()
