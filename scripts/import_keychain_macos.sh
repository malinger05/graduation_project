#!/usr/bin/env bash
# Import secrets into macOS Keychain via `security` CLI.
# Use when Python keyring hangs during bootstrap (no GUI prompt / stuck terminal).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE="atm-app"
ENV_FILE="${1:-$ROOT/.env.save}"

kc_set() {
  local name="$1" value="$2"
  [[ -z "$value" ]] && return 0
  security delete-generic-password -s "$SERVICE" -a "$name" >/dev/null 2>&1 || true
  security add-generic-password -s "$SERVICE" -a "$name" -w "$value" -U >/dev/null
  echo "  OK    $name"
}

read_env_var() {
  local key="$1" line
  line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" 2>/dev/null | tail -1 || true)"
  [[ -z "$line" ]] && return 0
  line="${line#*=}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  printf '%s' "$line"
}

echo "Importing keychain entries (service: $SERVICE)"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing file: $ENV_FILE" >&2
  exit 1
fi

GEN_FLASK="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))' 2>/dev/null || openssl rand -base64 36)"
kc_set "FLASK_SECRET_KEY" "$GEN_FLASK"
kc_set "MIDDLEWARE_DB_URL" "$(read_env_var MIDDLEWARE_DB_URL)"
kc_set "CONTRACT_ADDRESS" "$(read_env_var CONTRACT_ADDRESS)"
kc_set "ETH_PRIVATE_KEY" "$(read_env_var ETH_PRIVATE_KEY)"

TOKEN="$(read_env_var MIDDLEWARE_SERVICE_TOKEN)"
if [[ -z "$TOKEN" ]]; then
  TOKEN="dev-only-change-me"
  echo "  WARN  MIDDLEWARE_SERVICE_TOKEN not in $ENV_FILE — using local dev default"
fi
kc_set "MIDDLEWARE_SERVICE_TOKEN" "$TOKEN"

echo "Done. Verify: source atm_venv/bin/activate && python3 scripts/manage_secrets.py show FLASK_SECRET_KEY"
