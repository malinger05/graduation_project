import base64
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from argon2.low_level import Type as Argon2Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


DB_FILE = os.environ.get("ATM_SECURE_USER_DB", "secure_users.db").strip() or "secure_users.db"
KEY_FILE = os.environ.get("ATM_SECURE_USER_KEY", "user_db_aes256.key").strip() or "user_db_aes256.key"


class SecureUserDatabase:
    """
    SQLite user store: **Argon2id** for one-way secrets (PIN, fingerprint ID string),
    **AES-256-GCM** for reversible PII (name, email, phone) so the app can display them.
    """

    def __init__(self, db_path=DB_FILE, key_path=KEY_FILE):
        self.db_path = db_path
        self.key_path = key_path
        self.password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Argon2Type.ID,
        )
        self.lockout_minutes = [5, 10, 15]
        self.aes_key = self._load_or_create_key()
        self._initialize_schema()
        # Inserts demo users (1001/1234, …) only while the table is empty.
        self.seed_mock_users()

    def _load_or_create_key(self):
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as key_file:
                key = key_file.read()
                if len(key) != 32:
                    raise ValueError("AES key must be 32 bytes for AES-256.")
                return key

        key = AESGCM.generate_key(bit_length=256)
        with open(self.key_path, "wb") as key_file:
            key_file.write(key)
        print(f"✓ Created AES-256 key file: {self.key_path}")
        return key

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _initialize_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    account_number TEXT UNIQUE NOT NULL,
                    username_enc TEXT NOT NULL,
                    surname_enc TEXT NOT NULL,
                    full_name_enc TEXT NOT NULL,
                    email_enc TEXT,
                    phone_enc TEXT,
                    fingerprint_id_hash TEXT NOT NULL,
                    pin_hash TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT
                )
                """
            )
            self._migrate_users_columns(conn)
            conn.commit()

    @staticmethod
    def _migrate_users_columns(conn):
        existing = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "failed_attempts" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0")
        if "locked_until" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN locked_until TEXT")

    @staticmethod
    def _remaining_lock_seconds(locked_until):
        if not locked_until:
            return 0
        now = datetime.now(timezone.utc)
        if isinstance(locked_until, str):
            raw = locked_until.strip().replace("Z", "+00:00")
            locked_until = datetime.fromisoformat(raw)
        if getattr(locked_until, "tzinfo", None) is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        delta = (locked_until - now).total_seconds()
        return int(delta) if delta > 0 else 0

    def _encrypt_value(self, plaintext):
        if plaintext is None:
            return ""
        aesgcm = AESGCM(self.aes_key)
        nonce = os.urandom(12)
        encrypted = aesgcm.encrypt(nonce, str(plaintext).encode("utf-8"), None)
        return base64.b64encode(nonce + encrypted).decode("utf-8")

    def _decrypt_value(self, encrypted_value):
        if not encrypted_value:
            return ""
        raw = base64.b64decode(encrypted_value.encode("utf-8"))
        nonce = raw[:12]
        ciphertext = raw[12:]
        aesgcm = AESGCM(self.aes_key)
        decrypted = aesgcm.decrypt(nonce, ciphertext, None)
        return decrypted.decode("utf-8")

    @staticmethod
    def _is_argon_hash(value):
        return isinstance(value, str) and value.startswith("$argon2")

    def _hash_secret(self, value):
        return self.password_hasher.hash(str(value))

    def _verify_secret(self, stored_hash, provided_value):
        """
        Verify a stored Argon2 hash and optionally return a rehash value.
        Returns: (is_valid, upgraded_hash_or_none)
        """
        if not stored_hash:
            return False, None

        # PINs and biometric IDs must always be Argon2 hashes.
        if not self._is_argon_hash(stored_hash):
            return False, None
        try:
            is_valid = self.password_hasher.verify(stored_hash, str(provided_value))
            if not is_valid:
                return False, None
            if self.password_hasher.check_needs_rehash(stored_hash):
                return True, self._hash_secret(provided_value)
            return True, None
        except (VerifyMismatchError, VerificationError):
            return False, None

    def seed_mock_users(self):
        with self._connect() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
            if user_count > 0:
                return

            now = datetime.now().isoformat()
            seed_rows = [
                {
                    "user_id": "U1001",
                    "account_number": "1001",
                    "username": "John",
                    "surname": "Doe",
                    "email": "john.doe@example.com",
                    "phone": "+10000000001",
                    "pin": "1234",
                    "fingerprint_id": "FP-1001",
                    "balance": 500.0,
                },
                {
                    "user_id": "U1002",
                    "account_number": "1002",
                    "username": "Jane",
                    "surname": "Smith",
                    "email": "jane.smith@example.com",
                    "phone": "+10000000002",
                    "pin": "5678",
                    "fingerprint_id": "FP-1002",
                    "balance": 1000.0,
                },
                {
                    "user_id": "U1003",
                    "account_number": "1003",
                    "username": "Bob",
                    "surname": "Wilson",
                    "email": "bob.wilson@example.com",
                    "phone": "+10000000003",
                    "pin": "9012",
                    "fingerprint_id": "FP-1003",
                    "balance": 250.0,
                },
            ]

            for row in seed_rows:
                full_name = f"{row['username']} {row['surname']}"
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, account_number, username_enc, surname_enc, full_name_enc,
                        email_enc, phone_enc, fingerprint_id_hash, pin_hash, balance,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["user_id"],
                        row["account_number"],
                        self._encrypt_value(row["username"]),
                        self._encrypt_value(row["surname"]),
                        self._encrypt_value(full_name),
                        self._encrypt_value(row["email"]),
                        self._encrypt_value(row["phone"]),
                        self._hash_secret(row["fingerprint_id"]),
                        self._hash_secret(row["pin"]),
                        row["balance"],
                        now,
                        now,
                    ),
                )
            conn.commit()
        print("✓ Seeded secure mock users in SQLite database")

    def authenticate_with_status(self, account_id, pin):
        """Same contract as ``AccountsRepository.authenticate_with_status`` (web / CLI)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, account_number, full_name_enc, balance, pin_hash, is_active,
                       COALESCE(failed_attempts, 0), locked_until
                FROM users
                WHERE account_number = ?
                """,
                (account_id,),
            ).fetchone()

            if not row:
                return {"status": "invalid"}
            if row[5] != 1:
                return {"status": "invalid"}

            remaining_seconds = self._remaining_lock_seconds(row[7])
            if remaining_seconds > 0:
                return {
                    "status": "locked",
                    "remaining_lock_seconds": remaining_seconds,
                }

            stored_pin_hash = row[4]
            if not stored_pin_hash:
                return {"status": "invalid"}

            is_valid_pin, upgraded_pin_hash = self._verify_secret(stored_pin_hash, pin)
            if not is_valid_pin:
                current_failed = int(row[6] or 0)
                failed_attempts = current_failed + 1

                lock_minutes = 0
                new_locked_until = None
                if failed_attempts % 3 == 0:
                    lock_level = min((failed_attempts // 3), len(self.lockout_minutes))
                    lock_minutes = self.lockout_minutes[lock_level - 1]
                    new_locked_until = (datetime.now(timezone.utc) + timedelta(minutes=lock_minutes)).isoformat()

                conn.execute(
                    """
                    UPDATE users
                    SET failed_attempts = ?, locked_until = ?
                    WHERE account_number = ?
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

            if upgraded_pin_hash:
                conn.execute(
                    "UPDATE users SET pin_hash = ? WHERE account_number = ?",
                    (upgraded_pin_hash, account_id),
                )
            conn.execute(
                """
                UPDATE users
                SET failed_attempts = 0, locked_until = NULL
                WHERE account_number = ?
                """,
                (account_id,),
            )

            account = {
                "account_id": row[1],
                "name": self._decrypt_value(row[2]),
                "balance": float(row[3]),
            }
            return {"status": "ok", "account": account}

    def authenticate(self, account_id, pin):
        auth_result = self.authenticate_with_status(account_id, pin)
        if auth_result.get("status") != "ok":
            return None
        return auth_result.get("account")

    def get_balance(self, account_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT balance FROM users WHERE account_number = ? AND is_active = 1",
                (account_id,),
            ).fetchone()
            return float(row[0]) if row else 0.0

    def get_account(self, account_id):
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT account_number, full_name_enc, balance
                FROM users
                WHERE account_number = ? AND is_active = 1
                """,
                (account_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "account_id": row[0],
                "name": self._decrypt_value(row[1]),
                "balance": float(row[2]),
            }

    def apply_balance_change(self, account_id, delta):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT balance FROM users WHERE account_number = ? AND is_active = 1",
                (account_id,),
            ).fetchone()
            if not row:
                return None
            old_balance = float(row[0])
            new_balance = old_balance + float(delta)
            if new_balance < 0:
                return None
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE account_number = ?",
                (new_balance, now, account_id),
            )
            return old_balance, new_balance

    def list_users_public(self):
        """Decrypt names for admin/viewer tools (same shape as Postgres ``accounts`` listing)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT account_number, full_name_enc, balance
                FROM users
                WHERE is_active = 1
                ORDER BY account_number
                """
            ).fetchall()
        return [
            {"account_id": r[0], "name": self._decrypt_value(r[1]), "balance": float(r[2])}
            for r in rows
        ]

    def verify_credentials(self, account_number, pin):
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, account_number, full_name_enc, balance, pin_hash, is_active
                FROM users
                WHERE account_number = ?
                """,
                (account_number,),
            ).fetchone()

            if not row:
                return None
            if row[5] != 1:
                return None
            is_valid_pin, upgraded_pin_hash = self._verify_secret(row[4], pin)
            if not is_valid_pin:
                return None

            now = datetime.now().isoformat()
            if upgraded_pin_hash:
                conn.execute(
                    """
                    UPDATE users
                    SET pin_hash = ?, last_login_at = ?, updated_at = ?
                    WHERE account_number = ?
                    """,
                    (upgraded_pin_hash, now, now, account_number),
                )
            else:
                conn.execute(
                    "UPDATE users SET last_login_at = ?, updated_at = ? WHERE account_number = ?",
                    (now, now, account_number),
                )
            conn.commit()

            return {
                "user_id": row[0],
                "account_number": row[1],
                "full_name": self._decrypt_value(row[2]),
                "balance": float(row[3]),
            }

    def get_user_by_account(self, account_number):
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, account_number, full_name_enc, balance, is_active
                FROM users
                WHERE account_number = ?
                """,
                (account_number,),
            ).fetchone()
            if not row or row[4] != 1:
                return None
            return {
                "user_id": row[0],
                "account_number": row[1],
                "full_name": self._decrypt_value(row[2]),
                "balance": float(row[3]),
            }

    def update_balance(self, account_number, new_balance):
        with self._connect() as conn:
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE users SET balance = ?, updated_at = ? WHERE account_number = ?",
                (float(new_balance), now, account_number),
            )
            conn.commit()
