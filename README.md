# ATM with Blockchain Integrity Logging — 3-layer architecture

A 3-layer ATM application. The customer-facing UI and the middleware that
bridges to Core Banking live in this repo. The relational database and the
banking domain logic live in the **`core-banking-system`** sibling repo.

## Architecture

```
┌─────────────────────────┐   HTTP    ┌─────────────────────────────────┐   HTTP   ┌────────────────────────────┐
│ Layer 1                 │  ──────▶  │ Layer 2                         │ ──────▶  │ Layer 3                    │
│ customer_app.py (Flask) │           │ atm-middleware                  │          │ core-banking-system        │
│ atm_architecture.py     │           │ ├─ middleware.py (FastAPI)      │          │ Spring Boot + PostgreSQL   │
│ Pure UI / ATM client    │           │ ├─ blockchain_worker.py         │          │ Owns ALL data + logic.     │
│                         │           │ │   (3 reconciliation threads)  │          │ Exposes /admin endpoints   │
│                         │           │ ├─ admin_client.py              │          │ for the worker.            │
│                         │           │ └─ canonical.py                 │          │                            │
└─────────────────────────┘           └─────────────────────┬───────────┘          └────────────────────────────┘
                                                            │
                                                            ▼
                                                 ┌──────────────────────┐
                                                 │ Ethereum Sepolia     │
                                                 │ Canonical hash log   │
                                                 └──────────────────────┘
```

- **Layer 1 (`customer_app.py` + `atm_architecture.py`):** Flask UI and the
  ATM HTTP client. Knows nothing about databases or blockchain — just talks
  to the middleware.
- **Layer 2 (`atm-middleware/`):** FastAPI service on port 8000.
  - `middleware.py` — request bridge: forwards login / deposit / withdraw to
    Core Banking, manages in-memory sessions and lockouts, runs the withdraw
    atomicity timer, hashes the confirmed transaction, submits the hash to
    Sepolia, and PATCHes the result back into Core Banking.
  - `blockchain_worker.py` — three daemon threads spawned at startup:
    - **submit-retry** picks up rows with `chainStatus IN (PENDING_SUBMIT,
      FAILED_SUBMIT)` and retries blockchain submission.
    - **confirm-poll** polls Sepolia receipts and PATCHes rows in `SUBMITTED`
      to `CONFIRMED` once mined.
    - **tamper-check** recomputes the canonical hash from durable Core
      Banking fields and PATCHes `TAMPERED` if it disagrees with the stored
      hash (or with the on-chain `verifyLog`).
  - `admin_client.py` — HTTP client for `/admin/transactions/*`, authenticated
    with `X-Service-Token`.
  - `canonical.py` — single source of truth for the canonical payload + hash;
    used by both the inline flow and the worker.
  - **Holds no database of its own.**
- **Layer 3 (`core-banking-system`):** Spring Boot on port 8080 backed by
  PostgreSQL. Owns accounts, balances, transactions, BCrypt PIN verification,
  JWT issuance, and pessimistic locking. Adds new fields on `Transaction`
  (`canonicalHash`, `blockchainTx`, `chainStatus`, `submitAttempts`,
  `lastSubmitError`) and a service-token-gated `/admin/transactions/*` API
  consumed by the in-middleware worker.

## 1) Prerequisites

- Python 3.10+ (3.11+ recommended)
- Docker (for the Core Banking PostgreSQL container)
- Java 21 + Maven (for `core-banking-system`)
- A Sepolia wallet private key with test ETH
- A deployed contract address compatible with the project ABI

## 2) Clone and install dependencies

```bash
git clone <this repo>
cd graduation_project
python3 -m venv atm_venv
source atm_venv/bin/activate
pip install -r requirements.txt
```

## 3) Configure secrets and environment

### 3.1 Keyring (required for sensitive values)

Sensitive values are read **only** from the OS keychain — never from `.env`:

```bash
python3 scripts/manage_secrets.py set CONTRACT_ADDRESS
python3 scripts/manage_secrets.py set ETH_PRIVATE_KEY
python3 scripts/manage_secrets.py set FLASK_SECRET_KEY
python3 scripts/manage_secrets.py set MIDDLEWARE_SERVICE_TOKEN
```

`MIDDLEWARE_SERVICE_TOKEN` is the shared secret the in-middleware worker
sends as `X-Service-Token` to authenticate with Core Banking. The same value
must be set on the Spring Boot side via the `MIDDLEWARE_SERVICE_TOKEN`
environment variable (or `app.middleware.service-token` in
`application.properties`). For dev, the Spring Boot default is
`dev-only-change-me` — use that, or set both sides to the same fresh secret.

### 3.2 `.env` (non-sensitive only)

```bash
cp .env.example .env
```

Defaults already point at local Spring Boot (`http://localhost:8080`) and
the Sepolia RPC. Override `PORT` if Flask should not bind on `5000`.

## 4) Start Core Banking (Layer 3)

In the `core-banking-system` repo:

```bash
docker-compose up -d           # PostgreSQL on localhost:5332
./mvnw spring-boot:run         # Spring Boot on localhost:8080
```

Spring Boot's JPA `ddl-auto=create-drop` (dev) will create the new
`canonical_hash`, `blockchain_tx`, `chain_status`, `submit_attempts`, and
`last_submit_error` columns on the `transactions` table automatically.

## 5) Start the middleware (Layer 2)

```bash
source atm_venv/bin/activate
cd atm-middleware
python3 middleware.py          # FastAPI on localhost:8000
```

On startup the middleware logs whether the reconciliation worker started.
If you see `Worker NOT started — missing: ...`, set the missing keychain
entries above and restart.

## 6) Start the UI (Layer 1)

```bash
source atm_venv/bin/activate
honcho start                   # Flask web on PORT (default 5001)
```

Or directly:

```bash
python3 customer_app.py
```

## 7) Log in

Use credentials that exist in the Core Banking `accounts` table.

## 8) Expected behavior

- Withdraw / deposit goes UI → middleware → Spring Boot.
- Spring Boot acquires a pessimistic lock, updates the balance, and writes
  the transaction row to PostgreSQL with `chainStatus=PENDING_SUBMIT`.
- Middleware hashes the confirmed payload, submits the hash to Sepolia, and
  PATCHes the row to `chainStatus=SUBMITTED` with the on-chain tx hash.
- The **submit-retry** worker thread re-handles any rows still in
  `PENDING_SUBMIT` (e.g. inline submission failed because Sepolia was down).
- The **confirm-poll** worker thread upgrades rows to `CONFIRMED` once their
  Sepolia receipt indicates a successful mining.
- For withdrawals, middleware also starts a 30s ACK timer; if the ATM doesn't
  confirm cash dispense, the debit is reversed via Core Banking (atomicity).
- The dashboard shows a post-transaction QR popup linking to Etherscan for
  user self-verification.

## 9) Tamper detection

Performed by the **tamper-check** worker thread on `CONFIRMED` rows:

1. Fetch confirmed transactions from `/admin/transactions/for-tamper-check`.
2. Recompute the canonical hash from the row's durable fields
   (`accountNumber`, `transactionType`, `amount`, `balanceAfter`,
   `referenceId`, `createdAt`).
3. If the recomputed hash differs from the stored `canonicalHash`, PATCH the
   row to `chainStatus=TAMPERED`.
4. Optional secondary check: call the contract's `verifyLog(hash)` to confirm
   the hash is on chain.

Because the canonical payload uses only fields stored in Core Banking, any
direct mutation of the row (e.g. someone manually editing the DB) breaks
the hash and is caught on the next sweep.

## Repo layout

```
graduation_project/
├── customer_app.py              # Flask UI (Layer 1)
├── atm_architecture.py          # ATM HTTP client to middleware (Layer 1)
├── atm-middleware/              # Layer 2
│   ├── middleware.py            # FastAPI bridge + worker bootstrap
│   ├── blockchain_worker.py     # 3 reconciliation daemon threads
│   ├── admin_client.py          # HTTP client for Spring Boot /admin/transactions
│   └── canonical.py             # canonical payload + hash (shared)
├── secrets_manager.py           # OS keychain wrapper
├── scripts/manage_secrets.py
├── templates/, static/          # Flask templates and assets
├── tests/                       # pytest target
├── Procfile                     # honcho: web only (worker runs inside middleware)
└── requirements.txt
```
