# ATM with Blockchain + Indexer + PostgreSQL

This ATM project writes transaction integrity hashes to Ethereum Sepolia, stores transaction records in PostgreSQL, and provides both CLI and web customer flows.

## Architecture

- **Blockchain (Sepolia)**: immutable integrity log (`storeLog` / `verifyLog`).
- **Indexer**: confirms pending on-chain writes and monitors tampering.
- **PostgreSQL Transactions DB**: stores ATM transactions and statuses.
- **PostgreSQL Accounts DB**: stores account credentials and balances.

## 1) Prerequisites

Install these on your own machine:

- Python 3.10+ (3.11+ recommended)
- PostgreSQL 14+
- A Sepolia wallet private key with test ETH
- Deployed contract address (compatible with this app ABI)

## 2) Clone and install dependencies

```bash
git clone https://github.com/malinger05/ATM-with-blockchain-logging-fingerprint-authentication.git
cd ATM-with-blockchain-logging-fingerprint-authentication
python3 -m venv atm_venv
source atm_venv/bin/activate
pip install -r requirements.txt
```

## 3) Configure environment

Create `.env` from the example:

```bash
cp .env.example .env
```

Update `.env` values:

```env
CONTRACT_ADDRESS=0xYourSepoliaContract
ETH_PRIVATE_KEY=your_wallet_private_key_hex
DATABASE_URL=postgresql://localhost:5432/atm
ACCOUNTS_DATABASE_URL=postgresql://localhost:5432/atm
ACCOUNTS_TABLE=accounts
ETH_RPC_URL=https://ethereum-sepolia-rpc.publicnode.com
ETH_RPC_FALLBACK_URLS=https://sepolia.drpc.org,https://1rpc.io/sepolia
INDEXER_INTERVAL_SECONDS=3
FLASK_SECRET_KEY=use_a_long_random_string
```

Notes:
- Keep `.env` local only (never commit it).
- You can also store secrets in OS keychain via `scripts/manage_secrets.py`.

## 4) Create database and accounts table

Create the database:

```bash
createdb atm
```

Create accounts schema:

```bash
psql postgresql://localhost:5432/atm -c "
CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  pin_hash TEXT,
  pin TEXT,
  balance NUMERIC(14,2) NOT NULL DEFAULT 0,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  lockout_until TIMESTAMPTZ,
  lockout_level INTEGER NOT NULL DEFAULT 0
);"
```

Seed sample users (Argon2 PIN hashes):

```bash
python3 - <<'PY'
import os
import psycopg2
from argon2 import PasswordHasher
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")
db_url = os.environ.get("ACCOUNTS_DATABASE_URL") or os.environ.get("DATABASE_URL")
table = os.environ.get("ACCOUNTS_TABLE", "accounts")
ph = PasswordHasher()

rows = [
    ("1001", "John Doe", "1234", 500.00),
    ("1002", "Jane Smith", "5678", 1000.00),
    ("1003", "Bob Wilson", "9012", 250.00),
]

conn = psycopg2.connect(db_url)
conn.autocommit = True
with conn.cursor() as cur:
    for account_id, name, pin, balance in rows:
        cur.execute(
            f"""
            INSERT INTO {table} (account_id, name, pin_hash, balance)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id) DO UPDATE
            SET name = EXCLUDED.name,
                pin_hash = EXCLUDED.pin_hash,
                balance = EXCLUDED.balance
            """,
            (account_id, name, ph.hash(pin), balance),
        )
conn.close()
print("Seeded sample accounts.")
PY
```

## 5) Run the system

### Option A: CLI ATM

```bash
python3 atm.py
```

### Option B: Customer web UI

```bash
python3 customer_app.py
```

If port `5000` is already in use:

```bash
PORT=5001 python3 customer_app.py
```

Open `http://127.0.0.1:5000` (or your selected port).

## 6) Test login quickly

Use seeded sample credentials:

- `1001 / 1234`
- `1002 / 5678`
- `1003 / 9012`

## 7) What should happen

- Withdraw/deposit creates a pending transaction row.
- App sends canonical hash to Sepolia contract.
- Indexer confirms on-chain receipt and marks transaction `confirmed`.
- Dashboard shows post-transaction QR popup for user self-verification.

## Tamper detection behavior

Integrity verification checks:

1. Recomputed canonical hash equals DB hash.
2. Hash decoded from blockchain transaction input equals DB hash.
3. Contract lookup confirms hash exists.
4. Transaction status is confirmed by the indexer.
