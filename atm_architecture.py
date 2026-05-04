import csv
import hashlib
import inspect
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
import requests
from argon2 import PasswordHasher  # type: ignore[reportMissingImports]
from argon2.exceptions import VerificationError, VerifyMismatchError  # type: ignore[reportMissingImports]
from secrets_manager import get_secret
from secure_user_db import SecureUserDatabase

# Python 3.11+ removed inspect.getargspec, but older web3 dependency chains
# still import it indirectly (via parsimonious). Keep a compatibility alias.
if not hasattr(inspect, "getargspec"):
    inspect.getargspec = inspect.getfullargspec

from web3 import Web3
import qrcode

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

load_dotenv()

DEFAULT_TX_DATABASE_URL = "postgresql://localhost:5432/atm"
DATABASE_URL = get_secret("DATABASE_URL", DEFAULT_TX_DATABASE_URL).strip()

CONTRACT_ADDRESS = get_secret("CONTRACT_ADDRESS", "").strip()
ETH_PRIVATE_KEY = get_secret("ETH_PRIVATE_KEY", "").strip()
RPC_URL = os.environ.get("ETH_RPC_URL", "https://ethereum-sepolia.publicnode.com").strip()
RPC_FALLBACK_URLS = [
    url.strip()
    for url in os.environ.get("ETH_RPC_FALLBACK_URLS", "").split(",")
    if url.strip()
]
INDEXER_INTERVAL_SECONDS = float(os.environ.get("INDEXER_INTERVAL_SECONDS", "3").strip())

CONTRACT_ABI = [
    {
        "inputs": [{"internalType": "string", "name": "_transactionHash", "type": "string"}],
        "name": "storeLog",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "string", "name": "_logHash", "type": "string"}],
        "name": "verifyLog",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AccountsRepository:
    def __init__(self, database_url, table_name):
        self.conn = psycopg2.connect(database_url)
        self.conn.autocommit = True
        self.table_name = self._safe_identifier(table_name)
        self.pin_hasher = PasswordHasher()
        self.lockout_minutes = [5, 10, 15]
        self._ensure_auth_columns()

    @staticmethod
    def _safe_identifier(name):
        if not _VALID_IDENTIFIER.match(name):
            raise ValueError(f"Invalid SQL identifier: {name}")
        return name

    def _ensure_auth_columns(self):
        with self.conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE {self.table_name} ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0"
            )
            cur.execute(
                f"ALTER TABLE {self.table_name} ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ"
            )

    @staticmethod
    def _remaining_lock_seconds(locked_until):
        if not locked_until:
            return 0
        now = datetime.now(timezone.utc)
        if getattr(locked_until, "tzinfo", None) is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        delta = (locked_until - now).total_seconds()
        return int(delta) if delta > 0 else 0

    def authenticate_with_status(self, account_id, pin):
        query = f"""
            SELECT account_id, name, balance, COALESCE(pin_hash, pin) AS pin_hash,
                   COALESCE(failed_attempts, 0) AS failed_attempts, locked_until
            FROM {self.table_name}
            WHERE account_id=%s
        """
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (account_id,))
            account = cur.fetchone()
            if not account:
                return {"status": "invalid"}

            locked_until = account.get("locked_until")
            remaining_seconds = self._remaining_lock_seconds(locked_until)
            if remaining_seconds > 0:
                return {
                    "status": "locked",
                    "remaining_lock_seconds": remaining_seconds,
                }

            stored_pin_hash = account.get("pin_hash")
            if not stored_pin_hash:
                return {"status": "invalid"}

            try:
                self.pin_hasher.verify(str(stored_pin_hash), str(pin))
            except (VerifyMismatchError, VerificationError):
                current_failed = int(account.get("failed_attempts", 0) or 0)
                failed_attempts = current_failed + 1

                lock_minutes = 0
                new_locked_until = None
                if failed_attempts % 3 == 0:
                    lock_level = min((failed_attempts // 3), len(self.lockout_minutes))
                    lock_minutes = self.lockout_minutes[lock_level - 1]
                    new_locked_until = datetime.now(timezone.utc) + timedelta(minutes=lock_minutes)

                cur.execute(
                    f"""
                    UPDATE {self.table_name}
                    SET failed_attempts=%s, locked_until=%s
                    WHERE account_id=%s
                    """,
                    (failed_attempts, new_locked_until, account_id),
                )

                if lock_minutes:
                    return {
                        "status": "locked",
                        "remaining_lock_seconds": lock_minutes * 60,
                        "lock_minutes": lock_minutes,
                    }

                attempts_to_next_lock = 3 - (failed_attempts % 3)
                return {
                    "status": "invalid",
                    "attempts_to_next_lock": attempts_to_next_lock,
                }

            cur.execute(
                f"""
                UPDATE {self.table_name}
                SET failed_attempts=0, locked_until=NULL
                WHERE account_id=%s
                """,
                (account_id,),
            )

            account.pop("pin_hash", None)
            account.pop("failed_attempts", None)
            account.pop("locked_until", None)
            return {"status": "ok", "account": account}

    def authenticate(self, account_id, pin):
        auth_result = self.authenticate_with_status(account_id, pin)
        if auth_result.get("status") != "ok":
            return None
        return auth_result.get("account")

    def get_balance(self, account_id):
        query = f"SELECT balance FROM {self.table_name} WHERE account_id=%s"
        with self.conn.cursor() as cur:
            cur.execute(query, (account_id,))
            row = cur.fetchone()
            return float(row[0]) if row else 0.0

    def get_account(self, account_id):
        query = f"SELECT account_id, name, balance FROM {self.table_name} WHERE account_id=%s"
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (account_id,))
            return cur.fetchone()

    def apply_balance_change(self, account_id, delta):
        query = f"SELECT balance FROM {self.table_name} WHERE account_id=%s"
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (account_id,))
            row = cur.fetchone()
            if not row:
                return None
            old_balance = float(row["balance"])
            new_balance = old_balance + float(delta)
            if new_balance < 0:
                return None
            update_query = f"UPDATE {self.table_name} SET balance=%s WHERE account_id=%s"
            cur.execute(update_query, (new_balance, account_id))
            return old_balance, new_balance


class TransactionsRepository:
    def __init__(self, database_url):
        self.conn = psycopg2.connect(database_url)
        self.conn.autocommit = True
        self._init_schema()

    def _init_schema(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    amount NUMERIC(14,2) NOT NULL,
                    old_balance NUMERIC(14,2) NOT NULL,
                    new_balance NUMERIC(14,2) NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    canonical_hash TEXT NOT NULL,
                    blockchain_tx TEXT,
                    block_number BIGINT,
                    tx_index INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    confirmed_at TIMESTAMPTZ
                );
                """
            )
            # Backward-compatible schema evolution for safer local/chain state transitions.
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS error_reason TEXT")
            cur.execute(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0"
            )
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS operation_key TEXT")
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS last_retry_at TIMESTAMPTZ")
            cur.execute("UPDATE transactions SET status='queued_for_chain' WHERE status='pending'")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_transactions_status_retry
                ON transactions (status, next_retry_at, id)
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_operation_key
                ON transactions (operation_key)
                WHERE operation_key IS NOT NULL
                """
            )

    @staticmethod
    def _safe_identifier(name):
        if not _VALID_IDENTIFIER.match(name):
            raise ValueError(f"Invalid SQL identifier: {name}")
        return name

    def create_local_transaction_atomic(self, accounts_table, account_id, tx_type, amount, created_at):
        table = self._safe_identifier(accounts_table)
        previous_autocommit = self.conn.autocommit
        self.conn.autocommit = False
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"SELECT balance FROM {table} WHERE account_id=%s FOR UPDATE",
                    (account_id,),
                )
                row = cur.fetchone()
                if not row:
                    self.conn.rollback()
                    return None, "Account not found."

                old_balance = float(row["balance"])
                delta = -float(amount) if tx_type == "WITHDRAW" else float(amount)
                new_balance = old_balance + delta
                if new_balance < 0:
                    self.conn.rollback()
                    return None, "Insufficient funds"

                cur.execute(
                    f"UPDATE {table} SET balance=%s WHERE account_id=%s",
                    (new_balance, account_id),
                )

                payload = {
                    "account_id": account_id,
                    "type": tx_type,
                    "amount": float(amount),
                    "old_balance": old_balance,
                    "new_balance": new_balance,
                    "created_at": created_at,
                }
                payload["canonical_hash"] = canonical_hash(payload)
                operation_key = f"{account_id}:{payload['canonical_hash']}"
                cur.execute(
                    """
                    INSERT INTO transactions (
                        account_id, type, amount, old_balance, new_balance,
                        status, canonical_hash, blockchain_tx, created_at,
                        operation_key, retry_count, next_retry_at
                    ) VALUES (%s,%s,%s,%s,%s,'initiated',%s,NULL,%s,%s,0,NOW())
                    RETURNING id, account_id, type, amount, old_balance, new_balance, created_at, canonical_hash, status
                    """,
                    (
                        payload["account_id"],
                        payload["type"],
                        payload["amount"],
                        payload["old_balance"],
                        payload["new_balance"],
                        payload["canonical_hash"],
                        payload["created_at"],
                        operation_key,
                    ),
                )
                local_tx = cur.fetchone()
                self.conn.commit()
                return local_tx, ""
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.autocommit = previous_autocommit

    def _delete_transaction_by_id(self, tx_id):
        """Remove a row after a failed cross-store follow-up (best-effort)."""
        prev = self.conn.autocommit
        self.conn.autocommit = True
        try:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
        finally:
            self.conn.autocommit = prev

    def create_local_transaction_atomic_secure(
        self, secure_db, account_id, tx_type, amount, created_at
    ):
        """
        Insert the transaction in PostgreSQL first, then update SQLite under ``BEGIN IMMEDIATE``.
        If Postgres fails, the SQLite row lock is released without a balance change. If SQLite
        fails after Postgres commits, the orphan PG row is deleted so balances stay consistent.
        """
        previous_autocommit = self.conn.autocommit
        self.conn.autocommit = False
        sqlite_conn = None
        pg_committed_id = None
        try:
            sqlite_conn = secure_db._connect()
            sqlite_conn.execute("BEGIN IMMEDIATE")
            row = sqlite_conn.execute(
                """
                SELECT balance FROM users
                WHERE account_number = ? AND is_active = 1
                """,
                (account_id,),
            ).fetchone()
            if not row:
                sqlite_conn.rollback()
                self.conn.rollback()
                return None, "Account not found."

            old_balance = float(row[0])
            delta = -float(amount) if tx_type == "WITHDRAW" else float(amount)
            new_balance = old_balance + delta
            if new_balance < 0:
                sqlite_conn.rollback()
                self.conn.rollback()
                return None, "Insufficient funds"

            payload = {
                "account_id": account_id,
                "type": tx_type,
                "amount": float(amount),
                "old_balance": old_balance,
                "new_balance": new_balance,
                "created_at": created_at,
            }
            payload["canonical_hash"] = canonical_hash(payload)
            operation_key = f"{account_id}:{payload['canonical_hash']}"

            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO transactions (
                        account_id, type, amount, old_balance, new_balance,
                        status, canonical_hash, blockchain_tx, created_at,
                        operation_key, retry_count, next_retry_at
                    ) VALUES (%s,%s,%s,%s,%s,'initiated',%s,NULL,%s,%s,0,NOW())
                    RETURNING id, account_id, type, amount, old_balance, new_balance, created_at, canonical_hash, status
                    """,
                    (
                        payload["account_id"],
                        payload["type"],
                        payload["amount"],
                        payload["old_balance"],
                        payload["new_balance"],
                        payload["canonical_hash"],
                        payload["created_at"],
                        operation_key,
                    ),
                )
                local_tx = cur.fetchone()
            self.conn.commit()
            pg_committed_id = local_tx["id"]

            now = datetime.now().isoformat()
            sqlite_conn.execute(
                """
                UPDATE users SET balance = ?, updated_at = ?
                WHERE account_number = ? AND is_active = 1
                """,
                (new_balance, now, account_id),
            )
            sqlite_conn.commit()
            return local_tx, ""
        except Exception:
            if sqlite_conn:
                try:
                    sqlite_conn.rollback()
                except Exception:
                    pass
            try:
                self.conn.rollback()
            except Exception:
                pass
            if pg_committed_id is not None:
                try:
                    self._delete_transaction_by_id(pg_committed_id)
                except Exception:
                    pass
            raise
        finally:
            self.conn.autocommit = previous_autocommit
            if sqlite_conn:
                try:
                    sqlite_conn.close()
                except Exception:
                    pass

    def create_transaction(self, data):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO transactions (
                    account_id, type, amount, old_balance, new_balance,
                    status, canonical_hash, blockchain_tx, created_at
                ) VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s)
                RETURNING id
                """,
                (
                    data["account_id"],
                    data["type"],
                    data["amount"],
                    data["old_balance"],
                    data["new_balance"],
                    data["canonical_hash"],
                    data["blockchain_tx"],
                    data["created_at"],
                ),
            )
            return cur.fetchone()["id"]

    def update_transaction_confirmation(self, tx_id, block_number, tx_index):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE transactions
                SET status='confirmed', block_number=%s, tx_index=%s, confirmed_at=NOW(), error_reason=NULL
                WHERE id=%s
                """,
                (block_number, tx_index, tx_id),
            )

    def mark_transaction_submitted(self, tx_id, tx_hash):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE transactions
                SET blockchain_tx=%s, status='queued_for_chain', error_reason=NULL, next_retry_at=NULL
                WHERE id=%s
                """,
                (tx_hash, tx_id),
            )

    def mark_transaction_failed(self, tx_id, reason="Blockchain submission/confirmation failed"):
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE transactions SET status='failed', error_reason=%s WHERE id=%s",
                (reason, tx_id),
            )

    def schedule_submission_retry(self, tx_id, reason, delay_seconds=10):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE transactions
                SET status='failed',
                    error_reason=%s,
                    retry_count=retry_count+1,
                    last_retry_at=NOW(),
                    next_retry_at=NOW() + (%s * INTERVAL '1 second')
                WHERE id=%s
                """,
                (reason, int(delay_seconds), tx_id),
            )

    def mark_transaction_tampered(self, tx_id):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE transactions SET status='tampered' WHERE id=%s", (tx_id,))

    def get_pending_transactions(self):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, canonical_hash, blockchain_tx
                FROM transactions
                WHERE status IN ('queued_for_chain', 'pending') AND blockchain_tx IS NOT NULL
                ORDER BY id ASC
                """
            )
            return cur.fetchall()

    def get_transactions_for_submission(self, limit=25, max_retries=8):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, account_id, canonical_hash, retry_count
                FROM transactions
                WHERE blockchain_tx IS NULL
                  AND status IN ('initiated', 'failed')
                  AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                  AND retry_count < %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (max_retries, limit),
            )
            return cur.fetchall()

    def get_confirmed_transactions(self):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM transactions
                WHERE status='confirmed'
                ORDER BY id ASC
                """
            )
            return cur.fetchall()

    def get_transactions_for_account(self, account_id, limit=10):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM transactions WHERE account_id=%s ORDER BY created_at DESC LIMIT %s",
                (account_id, limit),
            )
            return cur.fetchall()

    def get_all_transactions(self):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM transactions ORDER BY id ASC")
            return cur.fetchall()

    def get_transaction_by_id(self, tx_id):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM transactions WHERE id=%s", (tx_id,))
            return cur.fetchone()

    def get_latest_transaction_for_account(self, account_id):
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM transactions
                WHERE account_id=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (account_id,),
            )
            return cur.fetchone()


class BlockchainGateway:
    def __init__(self):
        if not CONTRACT_ADDRESS or not ETH_PRIVATE_KEY:
            raise ValueError(
                "Missing CONTRACT_ADDRESS or ETH_PRIVATE_KEY. Set in keychain "
                "(preferred) or .env."
            )
        self.w3 = self._build_provider()
        self.account = self.w3.eth.account.from_key(ETH_PRIVATE_KEY)
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=CONTRACT_ABI,
        )

    @staticmethod
    def _provider_session():
        session = requests.Session()
        # Some environments export corporate proxy vars that block Sepolia.
        # Keep RPC calls direct by ignoring process proxy env.
        session.trust_env = False
        return session

    def _build_provider(self):
        candidates = [RPC_URL] + RPC_FALLBACK_URLS
        last_error = None
        for candidate in candidates:
            try:
                provider = Web3.HTTPProvider(
                    candidate,
                    request_kwargs={"timeout": 20},
                    session=self._provider_session(),
                )
                w3 = Web3(provider)
                # Force a test call to validate connectivity now.
                w3.eth.chain_id
                return w3
            except Exception as exc:
                last_error = exc
        raise ConnectionError(
            "Unable to reach Ethereum RPC. Set ETH_RPC_URL (and optional "
            "ETH_RPC_FALLBACK_URLS) in .env to reachable Sepolia endpoints."
        ) from last_error

    def submit_log_hash(self, canonical_hash):
        latest_block = self.w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas", self.w3.eth.gas_price)
        priority_fee = self.w3.to_wei(2, "gwei")
        max_fee = (2 * int(base_fee)) + int(priority_fee)
        tx = self.contract.functions.storeLog(canonical_hash).build_transaction(
            {
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(self.account.address),
                "gas": 200000,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority_fee,
                "type": 2,
                "chainId": self.w3.eth.chain_id,
            }
        )
        signed = self.account.sign_transaction(tx)
        return self.w3.eth.send_raw_transaction(signed.raw_transaction).hex()

    def verify_hash_in_contract(self, canonical_hash):
        return self.contract.functions.verifyLog(canonical_hash).call()

    def get_receipt(self, tx_hash):
        try:
            return self.w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return None

    def decode_stored_hash_from_tx(self, tx_hash):
        tx = self.w3.eth.get_transaction(tx_hash)
        fn, args = self.contract.decode_function_input(tx["input"])
        if fn.fn_name != "storeLog":
            return None
        return args.get("_transactionHash")


class Indexer:
    def __init__(self, transactions_repo, blockchain):
        self.transactions_repo = transactions_repo
        self.blockchain = blockchain
        self.alerted_tampered_ids = set()

    def sync_once(self):
        updated = 0
        for txn in self.transactions_repo.get_pending_transactions():
            receipt = self.blockchain.get_receipt(txn["blockchain_tx"])
            if not receipt:
                continue
            if receipt.status != 1:
                self.transactions_repo.mark_transaction_failed(txn["id"], "On-chain receipt status != 1")
                updated += 1
                continue
            on_chain_hash = self.blockchain.decode_stored_hash_from_tx(txn["blockchain_tx"])
            if on_chain_hash != txn["canonical_hash"]:
                self.transactions_repo.mark_transaction_failed(txn["id"], "On-chain hash mismatch during sync")
                updated += 1
                continue
            self.transactions_repo.update_transaction_confirmation(
                txn["id"], receipt.blockNumber, receipt.transactionIndex
            )
            updated += 1
        return updated

    def process_submission_retries_once(self):
        submitted = 0
        for txn in self.transactions_repo.get_transactions_for_submission():
            try:
                tx_hash = self.blockchain.submit_log_hash(txn["canonical_hash"])
                self.transactions_repo.mark_transaction_submitted(txn["id"], tx_hash)
                submitted += 1
            except Exception as exc:
                self.transactions_repo.schedule_submission_retry(txn["id"], str(exc), delay_seconds=10)
        return submitted

    @staticmethod
    def _normalize_created_at(created_at):
        if hasattr(created_at, "astimezone"):
            return created_at.astimezone(timezone.utc).isoformat()
        if hasattr(created_at, "isoformat"):
            return created_at.isoformat()
        return str(created_at)

    def _verify_integrity(self, txn):
        created_at = self._normalize_created_at(txn["created_at"])
        expected = canonical_hash(
            {
                "account_id": txn["account_id"],
                "type": txn["type"],
                "amount": float(txn["amount"]),
                "old_balance": float(txn["old_balance"]),
                "new_balance": float(txn["new_balance"]),
                "created_at": created_at,
            }
        )
        if expected != txn["canonical_hash"]:
            return False, "canonical hash mismatch"
        on_chain_hash = self.blockchain.decode_stored_hash_from_tx(txn["blockchain_tx"])
        if on_chain_hash != txn["canonical_hash"]:
            return False, "on-chain hash differs"
        if not self.blockchain.verify_hash_in_contract(txn["canonical_hash"]):
            return False, "hash missing in contract"
        return True, "ok"

    def monitor_tampering_once(self, report_fn=print):
        tampered_count = 0
        for txn in self.transactions_repo.get_confirmed_transactions():
            valid, reason = self._verify_integrity(txn)
            if valid:
                continue
            tx_id = txn["id"]
            self.transactions_repo.mark_transaction_tampered(tx_id)
            tampered_count += 1
            if tx_id in self.alerted_tampered_ids:
                continue
            self.alerted_tampered_ids.add(tx_id)
            report_fn(
                f"[ALERT] Tampering detected for transaction #{tx_id}: {reason} "
                f"(account={txn['account_id']}, blockchain_tx={txn['blockchain_tx']})"
            )
        return tampered_count

    def run_forever(self, interval_seconds=3):
        while True:
            try:
                self.process_submission_retries_once()
                self.sync_once()
                self.monitor_tampering_once()
            except Exception:
                pass
            time.sleep(interval_seconds)


class ATMApp:
    def __init__(self, accounts_repo, transactions_repo, blockchain, indexer):
        self.accounts_repo = accounts_repo
        self.transactions_repo = transactions_repo
        self.blockchain = blockchain
        self.indexer = indexer
        self.current_account = None

    @staticmethod
    def canonical_hash(payload):
        return canonical_hash(payload)

    def authenticate(self, account_id, pin):
        account = self.accounts_repo.authenticate(account_id, pin)
        if not account:
            print("Invalid credentials")
            return False
        self.current_account = account["account_id"]
        print(f"Welcome {account['name']}")
        return True

    def check_balance(self):
        return self.accounts_repo.get_balance(self.current_account)

    def _record(self, tx_type, amount):
        if hasattr(self.accounts_repo, "conn") and hasattr(self.transactions_repo, "conn"):
            if self.accounts_repo.conn.dsn != self.transactions_repo.conn.dsn:
                return (
                    False,
                    "Atomic mode requires the accounts and transactions repositories to use the same PostgreSQL database.",
                    False,
                )
        created_at = now_iso()
        if isinstance(self.accounts_repo, SecureUserDatabase):
            local_tx, err = self.transactions_repo.create_local_transaction_atomic_secure(
                self.accounts_repo, self.current_account, tx_type, float(amount), created_at
            )
        else:
            local_tx, err = self.transactions_repo.create_local_transaction_atomic(
                self.accounts_repo.table_name, self.current_account, tx_type, float(amount), created_at
            )
        if not local_tx:
            return False, err or "Unable to create local transaction", False

        tx_id = local_tx["id"]
        new_balance = float(local_tx["new_balance"])
        try:
            tx_hash = self.blockchain.submit_log_hash(local_tx["canonical_hash"])
            self.transactions_repo.mark_transaction_submitted(tx_id, tx_hash)
            return True, f"{tx_type} ${amount:.2f}. New balance: ${new_balance:.2f}", True
        except Exception as exc:
            self.transactions_repo.schedule_submission_retry(tx_id, str(exc), delay_seconds=10)
            return (
                True,
                (
                    f"{tx_type} ${amount:.2f}. New balance: ${new_balance:.2f}. "
                    "Recorded locally; blockchain sync will retry shortly."
                ),
                False,
            )

    def withdraw(self, amount):
        return self._record("WITHDRAW", amount) if amount > 0 else (False, "Amount must be positive", False)

    def deposit(self, amount):
        return self._record("DEPOSIT", amount) if amount > 0 else (False, "Amount must be positive", False)

    def verify_integrity(self, txn):
        created_at = txn["created_at"]
        if hasattr(created_at, "astimezone"):
            created_at = created_at.astimezone(timezone.utc).isoformat()
        elif hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        else:
            created_at = str(created_at)
        expected = self.canonical_hash(
            {
                "account_id": txn["account_id"],
                "type": txn["type"],
                "amount": float(txn["amount"]),
                "old_balance": float(txn["old_balance"]),
                "new_balance": float(txn["new_balance"]),
                "created_at": created_at,
            }
        )
        if expected != txn["canonical_hash"]:
            return False, "Tampered locally: canonical hash mismatch"
        if txn["status"] != "confirmed":
            return False, f"Not finalized (status={txn['status']})"
        if not txn.get("blockchain_tx"):
            return False, "Missing chain transaction reference"
        on_chain_hash = self.blockchain.decode_stored_hash_from_tx(txn["blockchain_tx"])
        if on_chain_hash != txn["canonical_hash"]:
            return False, "Tampered: on-chain hash differs"
        if not self.blockchain.verify_hash_in_contract(txn["canonical_hash"]):
            return False, "Not found in contract"
        return True, "Authentic and confirmed"

    def export_audit_report(self):
        rows = self.transactions_repo.get_all_transactions()
        filename = f"audit_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["id", "account_id", "type", "amount", "status", "canonical_hash", "blockchain_tx", "integrity"]
            )
            for txn in rows:
                valid, msg = self.verify_integrity(txn)
                writer.writerow(
                    [
                        txn["id"],
                        txn["account_id"],
                        txn["type"],
                        txn["amount"],
                        txn["status"],
                        txn["canonical_hash"],
                        txn["blockchain_tx"],
                        "OK" if valid else f"FAIL: {msg}",
                    ]
                )
        print(f"Audit report exported: {filename}")

    def generate_qr_receipt(self, txn):
        if not txn:
            print("No transaction found")
            return None
        tx_hash = txn.get("blockchain_tx")
        if not tx_hash:
            print("Cannot generate QR: transaction has no blockchain hash")
            return None
        verify_link = f"https://sepolia.etherscan.io/tx/{tx_hash}"
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(verify_link)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        created = txn.get("created_at")
        created_tag = (
            created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
            if hasattr(created, "astimezone")
            else str(created).replace(":", "-")
        )
        filename = f"receipt_{txn.get('type', 'TX')}_{float(txn.get('amount', 0))}_{created_tag}.png"
        image.save(filename)
        print(f"QR saved: {filename}")
        print(f"Scan/open: {verify_link}")
        return filename


def main():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed. Run: pip install psycopg2-binary")

    accounts_repo = SecureUserDatabase()
    transactions_repo = TransactionsRepository(DATABASE_URL)
    blockchain = BlockchainGateway()
    indexer = Indexer(transactions_repo, blockchain)
    atm = ATMApp(accounts_repo, transactions_repo, blockchain, indexer)

    while True:
        print("\n1. Login\n2. Exit")
        choice = input("Choose: ").strip()
        if choice == "1":
            if not atm.authenticate(input("Account: "), input("PIN: ")):
                continue
            while True:
                print(
                    f"\nBalance: ${atm.check_balance():.2f}\n"
                    "1. Withdraw\n2. Deposit\n3. Verify My Transactions\n4. Export Audit Report\n5. Show QR for Last Transaction\n6. Logout"
                )
                action = input("Choose: ").strip()
                if action == "1":
                    print(atm.withdraw(float(input("Withdraw amount: $")))[1])
                elif action == "2":
                    print(atm.deposit(float(input("Deposit amount: $")))[1])
                elif action == "3":
                    for txn in transactions_repo.get_transactions_for_account(atm.current_account, 50):
                        valid, status = atm.verify_integrity(txn)
                        print(f"#{txn['id']} -> {'OK' if valid else 'FAIL'}: {status}")
                elif action == "4":
                    atm.export_audit_report()
                elif action == "5":
                    txn = transactions_repo.get_latest_transaction_for_account(atm.current_account)
                    atm.generate_qr_receipt(txn)
                elif action == "6":
                    break
        elif choice == "2":
            break
        time.sleep(0.1)


if __name__ == "__main__":
    main()
