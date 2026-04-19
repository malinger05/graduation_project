import base64
import hashlib
import os
import sqlite3
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


DB_FILE = "secure_users.db"
KEY_FILE = "user_db_aes256.key"


class SecureUserDatabase:
    """SQLite user database with AES-256 encryption for sensitive columns."""

    def __init__(self, db_path=DB_FILE, key_path=KEY_FILE):
        self.db_path = db_path
        self.key_path = key_path
        self.aes_key = self._load_or_create_key()
        self._initialize_schema()
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
                    last_login_at TEXT
                )
                """
            )
            conn.commit()

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
    def _sha256(value):
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

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
                        self._sha256(row["fingerprint_id"]),
                        self._sha256(row["pin"]),
                        row["balance"],
                        now,
                        now,
                    ),
                )
            conn.commit()
        print("✓ Seeded secure mock users in SQLite database")

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
            if row[4] != self._sha256(pin):
                return None

            now = datetime.now().isoformat()
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
