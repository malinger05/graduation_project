# ATM with Blockchain + Indexer + PostgreSQL

This ATM project writes transaction integrity hashes to Ethereum Sepolia, stores transaction records in PostgreSQL, and provides a customer web flow.

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

## 3) Configure secrets and environment

This project uses **OS keychain for sensitive values** and `.env` for non-sensitive runtime options.

### 3.1 First-time keyring setup (required)

If you have never used keyring before, run:

```bash
python3 scripts/manage_secrets.py set CONTRACT_ADDRESS
python3 scripts/manage_secrets.py set ETH_PRIVATE_KEY
python3 scripts/manage_secrets.py set DATABASE_URL
python3 scripts/manage_secrets.py set ACCOUNTS_DATABASE_URL
python3 scripts/manage_secrets.py set FLASK_SECRET_KEY
```

You will be prompted for each value and they will be stored in your OS keychain (not in `.env`).

### 3.2 Configure `.env` for non-sensitive options

Create `.env` from the example:

```bash
cp .env.example .env
```

Set non-sensitive values in `.env`:

```env
ACCOUNTS_TABLE=accounts
ETH_RPC_URL=https://ethereum-sepolia-rpc.publicnode.com
ETH_RPC_FALLBACK_URLS=https://sepolia.drpc.org,https://1rpc.io/sepolia
INDEXER_INTERVAL_SECONDS=3
# PORT=5000
```

Important:
- Sensitive values are read from keychain in this project (`CONTRACT_ADDRESS`, `ETH_PRIVATE_KEY`, `DATABASE_URL`, `ACCOUNTS_DATABASE_URL`, `FLASK_SECRET_KEY`).
- Keep `.env` local only (never commit it).

## 4) Prepare the provided database

This project assumes you already have the provided PostgreSQL data (accounts + transactions schema/data).
After pulling the repository, restore/connect that DB on your machine and set:

- `DATABASE_URL` (in keychain)
- `ACCOUNTS_DATABASE_URL` (in keychain)
- `ACCOUNTS_TABLE`

If your DB backup uses a different database name or host, update the keychain values:

```bash
python3 scripts/manage_secrets.py set DATABASE_URL
python3 scripts/manage_secrets.py set ACCOUNTS_DATABASE_URL
```

## 5) Run the web app

```bash
python3 customer_app.py
```

If port `5000` is already in use:

```bash
PORT=5001 python3 customer_app.py
```

Open `http://127.0.0.1:5000` (or your selected port).

## 6) Login check

Use credentials that exist in your restored `accounts` table.

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
