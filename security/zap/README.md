# Safe OWASP ZAP — local demo stack

Use this only while **`scripts/run_demo.sh`** is running on your machine.

## Safe scope (do not exceed)

| Include | Exclude |
|---------|---------|
| `http://127.0.0.1:5001` (customer Flask, Layer 1) | Sepolia / any `*.publicnode.com` RPC |
| `https://atm.local` (optional, via Caddy) | `https://api.local` (Core Banking + mTLS) |
| Demo / test accounts only | `https://mw.local` unless you configure mTLS in ZAP |
| Local Docker Postgres on 5332/5433 | Production or teammate machines |

**Start with passive scan only.** Skip active scan on `/withdraw`, `/deposit`, and `/ack` until you use disposable test data.

---

## Phase 1 — Layer 1 passive scan (recommended first)

Simplest and safest: test the **customer UI** directly (no Caddy/mTLS).

### 1) Keep the stack running

Leave `scripts/run_demo.sh` open. Confirm: **Stack is up**.

### 2) Start OWASP ZAP

Open ZAP. Session: temporary is fine.

Confirm proxy: **Tools → Options → Network → Local Servers / Proxies** → add or edit a listener on **`127.0.0.1:8090`** (not 8080 — Core Banking uses 8080 in the demo stack). Enable it.

### 3) Open Chrome through ZAP (if ZAP “Open Browser” fails)

```bash
open -na "Google Chrome" --args \
  --proxy-server="127.0.0.1:8090" \
  --user-data-dir="/tmp/zap-chrome-profile"
```

For **HTTPS** via proxy later, install ZAP’s root CA in that Chrome profile:  
**Tools → Options → Network → Server Certificates → Save ZAP Root CA** → import into macOS Keychain (Always Trust) or Chrome.

### 4) Create ZAP context `local-atm`

- Include regex: `http://127\.0\.0\.1:5001.*`
- Enable **Show only URLs in scope**

### 5) Browse manually (passive)

In the proxied Chrome window:

1. `http://127.0.0.1:5001/atm`
2. Login flow (demo card/account from your seed data)
3. Dashboard, balance, transactions (read-only)
4. Logout

Do **not** run Active Scan yet. Check **Alerts** in ZAP.

### 6) Spider (in context)

Right-click site → **Attack → Spider** → context `local-atm` → Start.

### 7) Export report

**Report → Generate HTML Report** → save as `security/zap/report-YYYY-MM-DD.html`.

---

## Phase 2 — Customer via Caddy (optional)

Tests TLS + reverse proxy path.

1. Add context include: `https://atm\.local.*`
2. Use proxied Chrome; trust **mkcert** + ZAP CA if you want full HTTPS decode
3. Open `https://atm.local`
4. Passive browse only first

Still **exclude** `mw.local` and `api.local` unless you add client certificates to ZAP.

---

## Phase 3 — Active scan (optional, higher risk)

Only after passive review, with **test accounts** and **low** policy:

1. Exclude: `/withdraw`, `/deposit`, `/ack`, `/reset-pin`
2. Run Active Scan on context `local-atm` one section at a time
3. Stop if balances change unexpectedly or logs show errors

---

## Admin UI (separate, optional)

Scope: `http://127.0.0.1:5002` or `https://admin.local` — passive only unless you have admin test credentials.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| ZAP Sites empty | Use Chrome + `--proxy-server=127.0.0.1:8090`, not normal browser |
| ZAP can't bind 8080 | Core Banking uses :8080 — set ZAP proxy to **8090** instead |
| HTTPS errors | Use Phase 1 (`http://127.0.0.1:5001`) or install ZAP CA |
| App not loading | `scripts/kill_demo_ports.sh` then `scripts/run_demo.sh` |
| Too many external URLs | Turn on scope-only mode |

---

## After testing

Save the HTML report under `security/zap/`. Fix High/Medium findings in Flask/middleware before any active scan on production-like data.

Layer 1 Flask apps set security headers via `security_headers.py` (`CSP`, `X-Frame-Options`, `X-Content-Type-Options`, etc.). Re-run passive scan on `http://127.0.0.1:5001/atm` after pulling that change.

When testing via Caddy (`https://atm.local`), set `FLASK_SESSION_SECURE=1` for the customer app (and admin if scanned) so session cookies are `Secure`. Re-install the Caddyfile from `scripts/caddy/Caddyfile.example` if you use TLS termination there (`-Server` on the proxy).
