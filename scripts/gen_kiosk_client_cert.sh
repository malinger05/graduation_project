#!/usr/bin/env bash
# Create ATM kiosk client cert for https://mw.local mTLS (signed by mkcert CA).
# Re-issue: ./scripts/gen_kiosk_client_cert.sh --force
# Auto-renew: ./scripts/mtls/rotate_mtls_certs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/mtls/common.sh
source "$ROOT/scripts/mtls/common.sh"

FORCE=0
for arg in "$@"; do
  [[ "$arg" == "--force" ]] && FORCE=1
done

TLS_DIR="$(mtls_tls_dir)"
KEY="$TLS_DIR/atm-kiosk-client.key"
CERT="$TLS_DIR/atm-kiosk-client.pem"
VALIDITY="${MTLS_CERT_VALIDITY_DAYS:-825}"

if [[ -f "$CERT" && -f "$KEY" && "$FORCE" != "1" ]]; then
  echo "Kiosk client cert already exists:"
  echo "  $CERT"
  echo "  $KEY"
  echo "Re-issue: $0 --force   or: ./scripts/mtls/rotate_mtls_certs.sh --force"
  exit 0
fi

mtls_issue_client_cert "atm-kiosk" "$KEY" "$CERT" "$VALIDITY" 1
echo "Created kiosk mTLS client certificate (for customer_app → mw.local):"
echo "  $CERT"
echo "  $KEY"
echo ""
echo "Ensure Caddy mw.local requires client_auth (see scripts/caddy/Caddyfile.example)"
echo "Then restart Caddy: cd $TLS_DIR && sudo caddy run --config Caddyfile"
