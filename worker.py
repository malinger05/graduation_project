"""
Dedicated background worker for blockchain submission/confirmation and tamper checks.
Run in a separate process from the web or CLI app:
    python3 worker.py
"""

from atm_architecture import (
    DATABASE_URL,
    INDEXER_INTERVAL_SECONDS,
    BlockchainGateway,
    Indexer,
    TransactionsRepository,
)


def main():
    transactions_repo = TransactionsRepository(DATABASE_URL)
    blockchain = BlockchainGateway()
    indexer = Indexer(transactions_repo, blockchain)
    print(
        f"Indexer worker started. Polling every {INDEXER_INTERVAL_SECONDS:.1f}s "
        f"(DATABASE_URL={DATABASE_URL})"
    )
    indexer.run_forever(interval_seconds=INDEXER_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
