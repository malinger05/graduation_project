# ATM with Blockchain + Indexer + PostgreSQL

This project now uses a minimal event-sourced architecture:

- **Blockchain (Ethereum Sepolia)**: immutable integrity layer and event source.
- **Indexer**: continuously syncs pending blockchain writes and confirms them in storage.
- **PostgreSQL (Transactions DB)**: transaction/query store for indexer and audits.
- **External Accounts DB**: existing account source for auth and balance updates.

IPFS is no longer used.

## Secrets and Environment

Secrets are resolved in this order:

1. OS keychain via `keyring` (preferred)
2. `.env` / environment variables (fallback)

Store secrets in keychain:

```bash
python3 scripts/manage_secrets.py set CONTRACT_ADDRESS
python3 scripts/manage_secrets.py set ETH_PRIVATE_KEY
python3 scripts/manage_secrets.py set DATABASE_URL
python3 scripts/manage_secrets.py set ACCOUNTS_DATABASE_URL
python3 scripts/manage_secrets.py set FLASK_SECRET_KEY
```

Rotate quickly:

```bash
python3 scripts/manage_secrets.py delete ETH_PRIVATE_KEY
python3 scripts/manage_secrets.py set ETH_PRIVATE_KEY
```

Keep non-secret runtime options in `.env`, such as:

- `ACCOUNTS_TABLE`
- `ETH_RPC_URL`
- `ETH_RPC_FALLBACK_URLS`
- `INDEXER_INTERVAL_SECONDS`

### Legacy fallback (.env)

Required variables in `.env`:

- `CONTRACT_ADDRESS`
- `ETH_PRIVATE_KEY`
- `DATABASE_URL` (transactions DB URL, defaults to `postgresql://localhost:5432/atm`)
- `ACCOUNTS_DATABASE_URL` (optional; defaults to `DATABASE_URL`)
- `ACCOUNTS_TABLE` (optional; defaults to `accounts`)
- `ETH_RPC_URL` (optional)
- `INDEXER_INTERVAL_SECONDS` (optional; defaults to `3`)

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 atm.py
```

## Tamper Detection

Each transaction stores a canonical SHA-256 hash in PostgreSQL and writes the same hash on-chain using `storeLog`.
Integrity verification now checks:

1. Recomputed canonical hash equals DB hash.
2. Hash decoded from blockchain transaction input equals DB hash.
3. Contract-level lookup confirms hash presence.
4. Indexer marked transaction as `confirmed`.
