#!/usr/bin/env bash
#
# bootstrap.sh — one-time setup for a fresh clone.
#
# A `git pull` only brings the source code. The running stack also depends on
# machine-local material that is intentionally NOT committed:
#   - TLS certs under ~/atm-tls (server + mTLS client certs, Postgres certs)
#   - a local .env (gitignored)
#   - secrets in the OS keychain
#   - /etc/hosts entries for the *.local hostnames
#
# This script creates all of that so `scripts/run_demo.sh` can start the stack.
# It is safe to re-run: every step is idempotent and skips work already done.
#
# Usage:
#   scripts/bootstrap.sh             # interactive first-time setup
#   scripts/bootstrap.sh --yes       # assume "yes" for hosts edit prompt
#
# Overridable env:
#   ATM_TLS_DIR   TLS material dir (default: ~/atm-tls)
#   VENV_DIR      Python venv      (default: <repo>/atm_venv)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TLS_DIR="${ATM_TLS_DIR:-$HOME/atm-tls}"
VENV_DIR="${VENV_DIR:-$ROOT/atm_venv}"

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,24p'; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
ok()   { printf "  ${GREEN}OK${RESET}    %s\n" "$1"; }
warn() { printf "  ${YELLOW}WARN${RESET}  %s\n" "$1"; }
bad()  { printf "  ${RED}FAIL${RESET}  %s\n" "$1"; }
hdr()  { printf "\n${BOLD}%s${RESET}\n" "$1"; }
die()  { printf "${RED}${BOLD}%s${RESET}\n" "$1" >&2; exit 1; }

# ── 1. Required tools ─────────────────────────────────────────────────────────
hdr "[1/7] Checking required tools"
MISSING=0
for tool in docker java python3; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool"; else bad "$tool not found"; MISSING=1; fi
done
if command -v mkcert >/dev/null 2>&1; then
  ok "mkcert"
else
  bad "mkcert not found"
  echo "      Install: brew install mkcert nss && mkcert -install"
  MISSING=1
fi
if command -v caddy >/dev/null 2>&1; then ok "caddy"; else warn "caddy not found (needed for *.local; brew install caddy)"; fi
[[ "$MISSING" -eq 1 ]] && die "Install the missing tools above, then re-run bootstrap."

# Trust mkcert's root CA in the system store (idempotent).
mkcert -install >/dev/null 2>&1 || true
ok "mkcert root CA installed in system trust store"

# ── 2. Python venv + dependencies ─────────────────────────────────────────────
hdr "[2/7] Python virtualenv + dependencies"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
  ok "created venv at $VENV_DIR"
else
  ok "venv already exists at $VENV_DIR"
fi
PY="$VENV_DIR/bin/python"
"$PY" -m pip install --quiet --upgrade pip >/dev/null
"$PY" -m pip install --quiet -r "$ROOT/requirements.txt"
ok "dependencies installed from requirements.txt"

# ── 3. .env file ──────────────────────────────────────────────────────────────
hdr "[3/7] .env (non-sensitive config)"
if [[ -f "$ROOT/.env" ]]; then
  ok ".env already present"
else
  cp "$ROOT/.env.example" "$ROOT/.env"
  ok "created .env from .env.example"
fi

# ── 4. TLS material (~/atm-tls) ───────────────────────────────────────────────
hdr "[4/7] TLS certificates ($TLS_DIR)"
# Postgres server + CA — without these the Docker DB containers refuse to start.
bash "$ROOT/scripts/gen_postgres_server_cert.sh"  >/dev/null && ok "Postgres server cert + ca.pem"
bash "$ROOT/scripts/gen_kiosk_client_cert.sh"     >/dev/null && ok "kiosk mTLS client cert"
bash "$ROOT/scripts/gen_admin_client_cert.sh"     >/dev/null && ok "admin mTLS client cert"
bash "$ROOT/scripts/gen_mtls_client_cert.sh"      >/dev/null && ok "middleware mTLS client cert"
bash "$ROOT/scripts/caddy/install_caddyfile.sh"   >/dev/null && ok "Caddyfile + atm.local server cert"

# ── 5. Keychain secrets ───────────────────────────────────────────────────────
hdr "[5/7] Keychain secrets"
# Resolve current state through the project's own logic.
secret_is_set() {
  cd "$ROOT" && "$PY" - "$1" <<'PYEOF'
import sys
from secrets_manager import get_secret
sys.exit(0 if get_secret(sys.argv[1], "") else 1)
PYEOF
}
set_secret_value() {
  cd "$ROOT" && "$PY" scripts/manage_secrets.py set "$1" --value "$2" >/dev/null
}

# FLASK_SECRET_KEY: each machine can generate its own — no need to share.
if secret_is_set FLASK_SECRET_KEY; then
  ok "FLASK_SECRET_KEY already set"
else
  GEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  set_secret_value FLASK_SECRET_KEY "$GEN"
  ok "FLASK_SECRET_KEY generated and stored"
fi

# MIDDLEWARE_DB_URL: standard local value (Docker middleware Postgres on :5433).
# Setting it explicitly avoids the silent in-memory fallback.
if secret_is_set MIDDLEWARE_DB_URL; then
  ok "MIDDLEWARE_DB_URL already set"
else
  set_secret_value MIDDLEWARE_DB_URL "postgresql+psycopg://mwuser:mwpass@localhost:5433/mwdb"
  ok "MIDDLEWARE_DB_URL set to local default"
fi

# Shared team secrets: cannot be auto-generated. Prompt, allow skip.
for name in CONTRACT_ADDRESS ETH_PRIVATE_KEY MIDDLEWARE_SERVICE_TOKEN; do
  if secret_is_set "$name"; then
    ok "$name already set"
    continue
  fi
  printf "  Enter %s (from a teammate; leave blank to skip): " "$name"
  read -rs value; echo
  if [[ -n "$value" ]]; then
    set_secret_value "$name" "$value"
    ok "$name stored"
  else
    warn "$name skipped — set later: $PY scripts/manage_secrets.py set $name"
  fi
done

# ── 6. /etc/hosts ─────────────────────────────────────────────────────────────
hdr "[6/7] /etc/hosts hostnames"
HOSTS_LINE="127.0.0.1 atm.local admin.local mw.local api.local"
MISSING_HOSTS=()
for h in atm.local admin.local mw.local api.local; do
  grep -qE "[[:space:]]$h(\b|\$)" /etc/hosts 2>/dev/null || MISSING_HOSTS+=("$h")
done
if [[ "${#MISSING_HOSTS[@]}" -eq 0 ]]; then
  ok "all *.local hostnames already mapped"
else
  warn "missing: ${MISSING_HOSTS[*]}"
  DO_IT="$ASSUME_YES"
  if [[ "$DO_IT" -ne 1 ]]; then
    printf "  Append '%s' to /etc/hosts (needs sudo)? [y/N] " "$HOSTS_LINE"
    read -r ans
    [[ "$ans" =~ ^[Yy]$ ]] && DO_IT=1
  fi
  if [[ "$DO_IT" -eq 1 ]]; then
    echo "$HOSTS_LINE" | sudo tee -a /etc/hosts >/dev/null
    ok "appended to /etc/hosts"
  else
    warn "add manually: echo '$HOSTS_LINE' | sudo tee -a /etc/hosts"
  fi
fi

# ── 7. Done ───────────────────────────────────────────────────────────────────
hdr "[7/7] Bootstrap complete"
echo "Verify everything resolves:"
echo "  scripts/run_demo.sh --check"
echo
echo "Then start the full stack (Docker must be running):"
echo "  scripts/run_demo.sh"
echo
if [[ "${#MISSING_HOSTS[@]}" -ne 0 && "${DO_IT:-0}" -ne 1 ]]; then
  warn "Remember to add the /etc/hosts line above before running the demo."
fi
