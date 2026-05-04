# ATM with Blockchain + Indexer + PostgreSQL

This ATM project writes transaction integrity hashes to Ethereum Sepolia, stores transaction records in PostgreSQL, and provides a customer web flow.

## Architecture

- **Blockchain (Sepolia)**: immutable integrity log (`storeLog` / `verifyLog`).
- **Indexer worker** (`worker.py`): submission retries, on-chain confirmation sync, and tamper monitoring (runs in a separate process from the web UI).
- **PostgreSQL transactions DB**: ATM transactions and statuses.
- **SQLite users DB** (`secure_user_db.py`): customer credentials, encrypted PII fields, and balances. Paths: `ATM_SECURE_USER_DB` / `ATM_SECURE_USER_KEY` in `.env` (defaults: `secure_users.db`, `user_db_aes256.key`).

## Recent updates

- **`worker.py` + `Procfile`**: the indexer is a dedicated process; you can run it next to the Flask app manually or via a Procfile runner (see below).
- **Secrets**: sensitive values are read only from the **OS keychain** (`secrets_manager` / `scripts/manage_secrets.py`). `.env` is for **non-sensitive** options only—see `.env.example` (no private keys or DB passwords need to live in `.env`).
- **Web UI**: customer flows live in `customer_app.py` (Flask).
- **Tests**: `tests/` includes pytest coverage for transaction/indexer behavior.

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
source atm_venv/bin/activate  # Windows: atm_venv\Scripts\activate
pip install -r requirements.txt
```

## 3) Configure secrets and environment

### 3.1 Keyring (required for secrets)

Store sensitive values in the OS keychain (they are **not** read from `.env`):

```bash
python3 scripts/manage_secrets.py set CONTRACT_ADDRESS
python3 scripts/manage_secrets.py set ETH_PRIVATE_KEY
python3 scripts/manage_secrets.py set DATABASE_URL
python3 scripts/manage_secrets.py set FLASK_SECRET_KEY
```

### 3.2 `.env` (non-sensitive only)

```bash
cp .env.example .env
```

Edit `.env` for options such as RPC URLs, SQLite user DB paths, indexer interval, and optional `PORT`. Example:

```env
ETH_RPC_URL=https://ethereum-sepolia-rpc.publicnode.com
ETH_RPC_FALLBACK_URLS=https://sepolia.drpc.org,https://1rpc.io/sepolia
INDEXER_INTERVAL_SECONDS=3
PORT=5001
```

**Port note:** Flask defaults to `5000`. On macOS, AirPlay Receiver often uses that port; set `PORT=5001` (or another free port) in `.env` and open `http://127.0.0.1:5001`.

Keep `.env` local and out of version control.

## 4) Prepare the database

1. **PostgreSQL (transactions only):** set `DATABASE_URL` in the keychain to a database the app can reach. The worker and web app create/update the `transactions` table there on startup.

2. **SQLite (users):** the first time `users` is empty, the app inserts demo accounts (`1001` / PIN `1234`, `1002` / `5678`, `1003` / `9012`). Keep `user_db_aes256.key` with `secure_users.db`; without the key, encrypted fields cannot be decrypted.

```bash
python3 scripts/manage_secrets.py set DATABASE_URL
```

## 5) Run the application

You need **both** the web process and the worker for full behavior (submissions, confirmations, tamper checks).

### Option A — one command (recommended locally)

[`honcho`](https://github.com/nickstenning/honcho) reads the `Procfile`, loads `.env` into the environment for child processes, and starts **web** and **worker** together:

```bash
honcho start
```

Then open `http://127.0.0.1:<PORT>/` (default port `5000` if `PORT` is unset).

### Option B — two terminals

Terminal 1 (worker):

```bash
source atm_venv/bin/activate
python3 worker.py
```

Terminal 2 (web):

```bash
source atm_venv/bin/activate
python3 customer_app.py
```

`customer_app.py` respects the `PORT` environment variable (set in `.env` or inline: `PORT=5001 python3 customer_app.py`).

### Optional: run tests

```bash
source atm_venv/bin/activate
python3 -m pytest tests/
```

### Optional: inspect the database

`view_db.py` lists users from SQLite (`secure_user_db`) and recent rows from keychain-backed `DATABASE_URL`:

```bash
python3 view_db.py
```

## 6) Log in

Use credentials that exist in the SQLite `users` table (demo users are added automatically when the table was empty on first startup, or add rows yourself).

## 7) Expected behavior

- Withdraw/deposit creates a pending transaction row.
- The app sends the canonical hash to the Sepolia contract.
- The worker confirms on-chain receipt and marks the transaction `confirmed`.
- The dashboard can show a post-transaction QR popup for user self-verification.

## Tamper detection behavior

Integrity verification checks:

1. Recomputed canonical hash equals DB hash.
2. Hash decoded from blockchain transaction input equals DB hash.
3. Contract lookup confirms hash exists.
4. Transaction status is confirmed by the indexer.
