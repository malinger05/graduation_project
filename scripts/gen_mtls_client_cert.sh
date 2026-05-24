#!/usr/bin/env bash
# Create middleware client cert signed by mkcert's local CA (for api.local mTLS).
set -euo pipefail

TLS_DIR="${ATM_TLS_DIR:-$HOME/atm-tls}"
mkdir -p "$TLS_DIR"

if ! command -v mkcert >/dev/null 2>&1; then
  echo "mkcert is required. Install: brew install mkcert && mkcert -install"
  exit 1
fi

CAROOT="$(mkcert -CAROOT)"
CA_CERT="$CAROOT/rootCA.pem"
CA_KEY="$CAROOT/rootCA-key.pem"

if [[ ! -f "$CA_CERT" || ! -f "$CA_KEY" ]]; then
  echo "mkcert CA not found. Run: mkcert -install"
  exit 1
fi

KEY="$TLS_DIR/middleware-client.key"
CSR="$TLS_DIR/middleware-client.csr"
CERT="$TLS_DIR/middleware-client.pem"

if [[ -f "$CERT" && -f "$KEY" ]]; then
  echo "Client cert already exists:"
  echo "  $CERT"
  echo "  $KEY"
  exit 0
fi

openssl genrsa -out "$KEY" 2048
openssl req -new -key "$KEY" -out "$CSR" -subj "/CN=middleware"
openssl x509 -req -in "$CSR" -CA "$CA_CERT" -CAkey "$CA_KEY" \
  -CAcreateserial -out "$CERT" -days 825 -sha256
rm -f "$CSR"

chmod 600 "$KEY"
cp "$CA_CERT" "$TLS_DIR/mkcert-rootCA.pem"
echo "Created middleware mTLS client certificate:"
echo "  $CERT"
echo "  $KEY"
echo "CA copy for Caddy: $TLS_DIR/mkcert-rootCA.pem"
