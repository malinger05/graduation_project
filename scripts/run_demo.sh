#!/usr/bin/env bash
#
# run_demo.sh — one-command launcher for the full 3-layer ATM stack.
#
# Phase 1 (preflight): verify tools, Python venv, secrets, TLS certs, and
#   /etc/hosts. Fail fast with a clear message instead of starting a stack
#   that silently degrades (e.g. middleware running with in-memory sessions
#   because MIDDLEWARE_DB_URL was never resolved).
#
# Phase 2 (launch): start every layer in dependency order, wait for each to
#   become reachable, then stream a combined log until you press Ctrl-C.
#
# Usage:
#   scripts/run_demo.sh            # preflight, then start the whole stack
#   scripts/run_demo.sh --check    # preflight only (no services started)
#   scripts/run_demo.sh --no-caddy # skip Caddy (no sudo); use raw localhost ports
#
# Overridable env:
#   CORE_BANKING_DIR   path to core-banking-system (default: sibling of this repo)
#   ATM_TLS_DIR        TLS material dir (default: ~/atm-tls)
#   VENV_DIR           Python venv (default: <repo>/atm_venv)
set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CORE_BANKING_DIR="${CORE_BANKING_DIR:-$(cd "$ROOT/.." && pwd)/core-banking-system}"
TLS_DIR="${ATM_TLS_DIR:-$HOME/atm-tls}"
VENV_DIR="${VENV_DIR:-$ROOT/atm_venv}"
LOG_DIR="$ROOT/.demo-logs"

# ── Flags ─────────────────────────────────────────────────────────────────────
CHECK_ONLY=0
USE_CADDY=1
for arg in "$@"; do
  case "$arg" in
    --check|--check-only) CHECK_ONLY=1 ;;
    --no-caddy)           USE_CADDY=0 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,30p'
      exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# ── Output helpers ────────────────────────────────────────────────────────────
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
FAILURES=0
ok()   { printf "  ${GREEN}OK${RESET}    %s\n" "$1"; }
warn() { printf "  ${YELLOW}WARN${RESET}  %s\n" "$1"; }
bad()  { printf "  ${RED}FAIL${RESET}  %s\n" "$1"; FAILURES=$((FAILURES + 1)); }
hdr()  { printf "\n${BOLD}%s${RESET}\n" "$1"; }

# Resolve the Python interpreter (prefer the project venv).
if [[ -x "$VENV_DIR/bin/python" ]]; then
  PY="$VENV_DIR/bin/python"
else
  PY="$(command -v python3 || true)"
fi

# ── Phase 1: preflight ────────────────────────────────────────────────────────
hdr "Preflight — tools"
for tool in docker java python3; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool found"; else bad "$tool not found in PATH"; fi
done
if [[ -x "$CORE_BANKING_DIR/mvnw" ]]; then ok "core-banking mvnw found"; else bad "mvnw missing at $CORE_BANKING_DIR/mvnw"; fi
if [[ "$USE_CADDY" -eq 1 ]]; then
  if command -v caddy >/dev/null 2>&1; then ok "caddy found"; else bad "caddy not found (or run with --no-caddy)"; fi
fi

hdr "Preflight — Python environment"
if [[ -n "$PY" && -x "$PY" ]]; then
  ok "python: $PY"
  if "$PY" -c 'import fastapi, web3, sqlalchemy, keyring' >/dev/null 2>&1; then
    ok "core Python deps importable"
  else
    bad "Python deps missing — run: $PY -m pip install -r requirements.txt"
  fi
else
  bad "no usable Python interpreter (expected venv at $VENV_DIR)"
fi

hdr "Preflight — secrets (keychain first, .env fallback where allowed)"
# Resolve every secret through the project's own logic so the check matches
# what the running services will actually see. .env is loaded first so the
# non-sensitive fallbacks (e.g. MIDDLEWARE_DB_URL) resolve exactly as at runtime.
if [[ -n "$PY" && -x "$PY" ]]; then
  SECRET_REPORT="$(
    cd "$ROOT" && "$PY" - <<'PYEOF'
import sys
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
from secrets_manager import get_secret

# name -> required?  (sensitive secrets are keychain-only)
checks = [
    ("CONTRACT_ADDRESS", True),
    ("ETH_PRIVATE_KEY", True),
    ("FLASK_SECRET_KEY", True),
    ("MIDDLEWARE_SERVICE_TOKEN", True),
    ("MIDDLEWARE_DB_URL", True),
]
for name, required in checks:
    val = get_secret(name, "")
    status = "set" if val else "missing"
    print(f"{name}\t{status}\t{int(required)}")
PYEOF
  )" || SECRET_REPORT=""
  if [[ -z "$SECRET_REPORT" ]]; then
    bad "could not evaluate secrets (Python error)"
  else
    while IFS=$'\t' read -r name status required; do
      [[ -z "$name" ]] && continue
      if [[ "$status" == "set" ]]; then
        ok "$name resolved"
      elif [[ "$required" == "1" ]]; then
        bad "$name not set (keychain: python3 scripts/manage_secrets.py set $name)"
      else
        warn "$name not set (optional)"
      fi
    done <<< "$SECRET_REPORT"
  fi
else
  bad "skipping secret check — no Python interpreter"
fi

hdr "Preflight — TLS material ($TLS_DIR)"
check_file() { if [[ -f "$1" ]]; then ok "$2"; else bad "$2 missing: $1"; fi; }
check_server_cert() {
  # $1 = label, $2 = remediation. Pick newest atm.local+N.pem, skip *-key.pem.
  local match=""
  for f in $(ls -1 "$TLS_DIR"/atm.local*.pem 2>/dev/null | sort -V); do
    case "$f" in *-key.pem) continue ;; esac
    match="$f"
  done
  if [[ -n "$match" ]]; then ok "$1 ($match)"; else bad "$1 missing ($2)"; fi
}
check_file "$TLS_DIR/mkcert-rootCA.pem"        "mkcert CA copy"          # gen scripts / install_caddyfile.sh
check_file "$TLS_DIR/atm-kiosk-client.pem"     "kiosk client cert"      # gen_kiosk_client_cert.sh
check_file "$TLS_DIR/atm-kiosk-client.key"     "kiosk client key"
check_file "$TLS_DIR/admin-staff-client.pem"   "admin client cert"      # gen_admin_client_cert.sh
check_file "$TLS_DIR/middleware-client.pem"    "middleware client cert" # gen_mtls_client_cert.sh
check_file "$TLS_DIR/postgres/ca.pem"          "postgres TLS CA"        # gen_postgres_server_cert.sh
if [[ "$USE_CADDY" -eq 1 ]]; then
  check_file "$TLS_DIR/Caddyfile"              "Caddyfile"              # scripts/caddy/install_caddyfile.sh
  check_server_cert                            "server cert (atm.local)" "scripts/caddy/install_caddyfile.sh"
fi

hdr "Preflight — /etc/hosts"
for host in atm.local admin.local mw.local api.local; do
  if grep -qE "[[:space:]]$host(\b|\$)" /etc/hosts 2>/dev/null; then
    ok "$host mapped"
  else
    bad "$host missing — add: 127.0.0.1 atm.local admin.local mw.local api.local"
  fi
done

# ── Preflight verdict ─────────────────────────────────────────────────────────
echo
if [[ "$FAILURES" -gt 0 ]]; then
  printf "${RED}${BOLD}Preflight failed: %d problem(s).${RESET} Fix the items above and re-run.\n" "$FAILURES"
  exit 1
fi
printf "${GREEN}${BOLD}Preflight passed.${RESET}\n"
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "(--check) Not starting services."
  exit 0
fi

# ── Phase 2: launch ───────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
PIDS=()
CADDY_PID=""

cleanup() {
  echo
  hdr "Shutting down..."
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" >/dev/null 2>&1 || true
  done
  if [[ -n "$CADDY_PID" ]]; then sudo kill "$CADDY_PID" >/dev/null 2>&1 || true; fi
  # Leave the Docker databases running; they are cheap and reused across demos.
  echo "Stopped app processes. Postgres containers left running (docker compose down to stop)."
}
trap cleanup EXIT INT TERM

wait_tcp() { # host port timeout_s
  local host="$1" port="$2" timeout="${3:-60}" i
  for ((i = 0; i < timeout; i++)); do
    (exec 3<>"/dev/tcp/$host/$port") >/dev/null 2>&1 && { exec 3>&- 3<&-; return 0; }
    sleep 1
  done
  return 1
}

wait_http() { # url timeout_s
  local url="$1" timeout="${2:-60}" i
  for ((i = 0; i < timeout; i++)); do
    curl -sk -o /dev/null --max-time 3 "$url" && return 0
    sleep 1
  done
  return 1
}

# 1) Databases (Core Banking + middleware Postgres, both TLS) ───────────────────
hdr "[1/6] Starting Postgres (docker compose)"
( cd "$CORE_BANKING_DIR" && docker compose up -d ) >"$LOG_DIR/docker.log" 2>&1
if wait_tcp 127.0.0.1 5332 60 && wait_tcp 127.0.0.1 5433 60; then
  ok "Postgres up (5332 core-banking, 5433 middleware)"
else
  bad "Postgres did not become reachable — see $LOG_DIR/docker.log"; exit 1
fi

# 2) Core Banking (Spring Boot) ────────────────────────────────────────────────
hdr "[2/6] Starting Core Banking (Spring Boot :8080)"
export MIDDLEWARE_SERVICE_TOKEN="$(cd "$ROOT" && "$PY" -c 'from secrets_manager import get_secret; print(get_secret("MIDDLEWARE_SERVICE_TOKEN",""))')"
( cd "$CORE_BANKING_DIR" && ./mvnw -q spring-boot:run ) >"$LOG_DIR/core-banking.log" 2>&1 &
PIDS+=("$!")
if wait_tcp 127.0.0.1 8080 180; then
  ok "Core Banking listening on :8080"
else
  bad "Core Banking did not start — see $LOG_DIR/core-banking.log"; exit 1
fi

# 3) Middleware (FastAPI) ───────────────────────────────────────────────────────
hdr "[3/6] Starting middleware (FastAPI :8000)"
( cd "$ROOT/atm-middleware" && "$PY" middleware.py ) >"$LOG_DIR/middleware.log" 2>&1 &
PIDS+=("$!")
if wait_http "http://127.0.0.1:8000/health" 60; then
  ok "Middleware healthy on :8000"
else
  bad "Middleware /health not responding — see $LOG_DIR/middleware.log"; exit 1
fi
if grep -q "Operational DB: disabled" "$LOG_DIR/middleware.log" 2>/dev/null; then
  warn "Middleware started WITHOUT a database (in-memory sessions/lockouts). Check MIDDLEWARE_DB_URL."
fi

# 4) Caddy (reverse proxy + mTLS) ──────────────────────────────────────────────
if [[ "$USE_CADDY" -eq 1 ]]; then
  hdr "[4/6] Starting Caddy (sudo; *.local on :443)"
  echo "  Caddy binds privileged port 443 — you may be prompted for your password."
  sudo caddy start --config "$TLS_DIR/Caddyfile" >"$LOG_DIR/caddy.log" 2>&1 || {
    bad "Caddy failed to start — see $LOG_DIR/caddy.log"; exit 1
  }
  CADDY_PID="$(sudo lsof -tiTCP:443 -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  if wait_tcp 127.0.0.1 443 30; then ok "Caddy serving HTTPS on :443"; else warn "Caddy :443 not confirmed — see $LOG_DIR/caddy.log"; fi
else
  hdr "[4/6] Caddy skipped (--no-caddy)"
  warn "Without Caddy, *.local mTLS hostnames are unavailable; use raw localhost ports."
fi

# 5) Customer UI ───────────────────────────────────────────────────────────────
hdr "[5/6] Starting customer UI (Flask :5001)"
( cd "$ROOT" && PORT=5001 "$PY" customer_app.py ) >"$LOG_DIR/customer.log" 2>&1 &
PIDS+=("$!")
if wait_tcp 127.0.0.1 5001 30; then ok "Customer UI on :5001"; else bad "Customer UI did not start — see $LOG_DIR/customer.log"; exit 1; fi

# 6) Admin UI ──────────────────────────────────────────────────────────────────
hdr "[6/6] Starting admin UI (Flask :5002)"
( cd "$ROOT" && PORT=5002 "$PY" admin-app/admin_app.py ) >"$LOG_DIR/admin.log" 2>&1 &
PIDS+=("$!")
if wait_tcp 127.0.0.1 5002 30; then ok "Admin UI on :5002"; else bad "Admin UI did not start — see $LOG_DIR/admin.log"; exit 1; fi

# ── Ready ─────────────────────────────────────────────────────────────────────
hdr "Stack is up"
if [[ "$USE_CADDY" -eq 1 ]]; then
  echo "  Customer:   https://atm.local"
  echo "  Admin:      https://admin.local"
  echo "  Middleware: https://mw.local  (kiosk/admin mTLS)"
  echo "  Core Bank:  https://api.local (middleware mTLS)"
else
  echo "  Customer:   http://127.0.0.1:5001"
  echo "  Admin:      http://127.0.0.1:5002"
  echo "  Middleware: http://127.0.0.1:8000"
  echo "  Core Bank:  http://127.0.0.1:8080"
fi
echo "  Logs:       $LOG_DIR/*.log"
echo
echo "Press Ctrl-C to stop the app processes (Postgres containers stay up)."
echo

# Stream combined logs until interrupted.
tail -n +1 -F "$LOG_DIR"/middleware.log "$LOG_DIR"/customer.log "$LOG_DIR"/admin.log 2>/dev/null &
PIDS+=("$!")
wait
