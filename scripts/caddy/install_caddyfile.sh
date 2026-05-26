#!/usr/bin/env bash
# Install Caddy reverse-proxy config for local ATM stack.
set -euo pipefail

TLS_DIR="${ATM_TLS_DIR:-$HOME/atm-tls}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE="$SCRIPT_DIR/Caddyfile.example"
DEST="$TLS_DIR/Caddyfile"

mkdir -p "$TLS_DIR"

if ! command -v mkcert >/dev/null 2>&1; then
  echo "Install mkcert: brew install mkcert && mkcert -install"
  exit 1
fi

# Prefer newest atm.local+N.pem (not *-key.pem, not kiosk/middleware certs)
CERT=""
for f in $(ls -1 "$TLS_DIR"/atm.local+*.pem 2>/dev/null | sort -V); do
  case "$f" in
    *-key.pem) continue ;;
    atm-kiosk-client.pem|middleware-client.pem) continue ;;
  esac
  CERT="$f"
done

KEY=""
if [[ -n "$CERT" ]]; then
  KEY="${CERT%.pem}-key.pem"
fi

if [[ -z "$CERT" || ! -f "$KEY" ]]; then
  CERT="$TLS_DIR/atm.local.pem"
  KEY="$TLS_DIR/atm.local-key.pem"
  echo "Creating TLS cert for atm.local admin.local mw.local api.local ..."
  mkcert -cert-file "$CERT" -key-file "$KEY" atm.local admin.local mw.local api.local
fi

CA="$TLS_DIR/mkcert-rootCA.pem"
if [[ ! -f "$CA" ]]; then
  CAROOT="$(mkcert -CAROOT)"
  cp "$CAROOT/rootCA.pem" "$CA"
fi

sed \
  -e "s|__ATM_TLS_CERT__|$CERT|g" \
  -e "s|__ATM_TLS_KEY__|$KEY|g" \
  -e "s|__MKCERT_CA__|$CA|g" \
  "$EXAMPLE" > "$DEST"

echo "Wrote $DEST"
echo "  cert: $CERT"
echo "  key:  $KEY"
echo ""
echo "Add to /etc/hosts if missing:"
echo "  127.0.0.1 atm.local admin.local mw.local api.local"
echo ""
echo "Kiosk mTLS (customer_app → mw.local):"
echo "  ./scripts/gen_kiosk_client_cert.sh"
echo ""
echo "mw.local forwards client cert serial/subject to middleware (audit allow-list)."
echo "  ./scripts/list_client_cert_serials.sh"
echo ""
echo "Start Caddy:"
echo "  cd $TLS_DIR && sudo caddy run --config Caddyfile"
