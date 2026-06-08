# Load & performance testing (Locust)

Measures throughput and latency of the ATM **middleware** (Layer 2) and, through
it, Core Banking (Layer 3). Because each deposit/withdraw submits the canonical
hash to Sepolia **inline** (`_hash_and_persist` → `_submit_to_blockchain` in
`atm-middleware/middleware.py`), write latency is dominated by the blockchain
RPC — so we test layers in isolation as well as end-to-end.

## Layout

| File | Purpose |
|------|---------|
| `common.py` | shared config (target URLs, mTLS material, account/session loading) |
| `seed_accounts.py` | creates N throwaway accounts (customer + account + card + PIN) and writes `accounts.json` |
| `warm_sessions.py` | logs in a few accounts slowly and writes reusable `sessions.json` |
| `locustfile.py` | the load scenarios (read / write / contention / reversal) |
| `requirements.txt` | `locust`, `requests` |

> **Why a separate login step?** Core Banking rate-limits `/atm/login` to
> **10/min per IP** (anti PIN brute force), and all middleware traffic shares
> one IP. Logging in 100 users at once gets throttled (`429` → middleware `502`).
> So we log in a few accounts **once, slowly**, reuse those sessions during the
> run, and put the load on the endpoints that are NOT rate-limited
> (deposit/withdraw/balance).

## 1. Install

```bash
pip install -r loadtest/requirements.txt
```

## 2. Start the stack

The full stack must be running (`scripts/run_demo.sh`). For seeding, Core
Banking must be reachable (loopback `:8080` works without mTLS).

## 3. Seed test accounts (once)

```bash
python loadtest/seed_accounts.py --count 50
```

Creates 50 accounts, each with a large balance and PIN `1234`, and writes
`loadtest/accounts.json`. Re-run with `--append` to add more. Uses Core Banking
admin creds (`CB_ADMIN_USERNAME`/`CB_ADMIN_PASSWORD`, default `admin`/`admin123`).

## 4. Warm up sessions (once, right before a run)

```bash
python loadtest/warm_sessions.py --count 15
```

Logs in 15 accounts at ~1 every 15s (under the 10/min limit; takes ~3-4 min)
and writes `loadtest/sessions.json`. The load test reuses these tokens, so no
logins happen during the run. Sessions stay alive while in use — run the load
test soon after. If you see "rate limited", it waits 60s and retries.

## 5. Run the scenarios

Choose a **target** to control what you isolate:

```bash
export MW_HOST=http://127.0.0.1:8000   # app + inline blockchain (no TLS)
# export MW_HOST=https://mw.local      # realistic: through Caddy mTLS
```

### A. Read throughput (baseline ceiling)
Default task mix is read-heavy already; just run it:
```bash
locust -f loadtest/locustfile.py --host $MW_HOST \
       --headless -u 100 -r 10 -t 3m --csv=loadtest/results_read
```

### B. Write throughput — different accounts (parallelism)
```bash
SAME_ACCOUNT=0 locust -f loadtest/locustfile.py --host $MW_HOST \
       --headless -u 100 -r 10 -t 5m --csv=loadtest/results_write
```

### C. Lock contention — one account (serialization)
All users transact the **same** account, exercising Core Banking's pessimistic
lock. Compare p95 against scenario B.
```bash
SAME_ACCOUNT=1 locust -f loadtest/locustfile.py --host $MW_HOST \
       --headless -u 100 -r 10 -t 5m --csv=loadtest/results_contention
```

### D. Quantify the inline blockchain cost
Run scenario B twice — once with Sepolia reachable, once with the chain
disabled (point the RPC at an unreachable URL so `_submit_to_blockchain`
fails fast and the row stays `PENDING_SUBMIT` for the worker):
```bash
# chain "off": middleware returns immediately, worker retries later
ETH_RPC_URL=http://127.0.0.1:1  ETH_RPC_FALLBACK_URLS= \
  <restart middleware>  # then run scenario B and compare write p95
```

### E. mTLS overhead
Run scenario A against `http://127.0.0.1:8000` and again against
`https://mw.local`; the delta is the TLS-handshake/proxy cost.

### F. Reversal under load (atomicity)
Skip the dispense ACK so Core Banking's reversal scheduler kicks in:
```bash
ACK_DISABLE=1 SAME_ACCOUNT=0 locust -f loadtest/locustfile.py --host $MW_HOST \
       --headless -u 50 -r 10 -t 3m
```

## 6. Interactive UI (optional)

Drop `--headless ...` to open the Locust web UI at http://localhost:8089 and
drive users/spawn-rate manually with live charts.

## 7. What to capture for the report

- p50/p95/p99 latency + req/s per endpoint (from the `--csv` `*_stats.csv`).
- **Read vs write** and **write with vs without inline blockchain** (the
  headline finding).
- **Contention (C) vs parallel (B)** p95 — evidences pessimistic-lock behavior.
- Server-side: middleware CPU and Postgres lock waits
  (`SELECT wait_event, count(*) FROM pg_stat_activity GROUP BY 1;`).

## Notes & caveats

- Each financial request sends a fresh `Idempotency-Key`; reusing one returns
  the cached response and does no real work (by design).
- Seeded accounts have huge balances so withdrawals don't deplete them; a `400`
  on withdraw means insufficient funds (only likely in long contention runs).
- These are **throwaway** accounts in your dev DB. Don't run against anything
  real. `accounts.json` and `sessions.json` are gitignored.
- The load test does **not** log in (login is rate-limited). If you see a flood
  of failures, your `sessions.json` is stale/expired — re-run `warm_sessions.py`.
