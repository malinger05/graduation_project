#!/usr/bin/env bash
# Server TLS cert for local PostgreSQL (Docker). Clients trust ca.pem (mkcert root).
set -euo pipefail

TLS_DIR="${ATM_TLS_POSTGRES_DIR:-${ATM_TLS_DIR:-$HOME/atm-tls}/postgres}"
mkdir -p "$TLS_DIR"

if ! command -v mkcert >/dev/null 2>&1; then
  echo "mkcert is required. Install: brew install mkcert && mkcert -install"
  exit 1
fi

mkcert -install >/dev/null 2>&1 || true

mkcert -cert-file "$TLS_DIR/server.crt" -key-file "$TLS_DIR/server.key" \
  localhost 127.0.0.1 ::1
chmod 600 "$TLS_DIR/server.key"

CAROOT="$(mkcert -CAROOT)"
cp "$CAROOT/rootCA.pem" "$TLS_DIR/ca.pem"

echo "PostgreSQL TLS material (Docker + app clients):"
echo "  $TLS_DIR/server.crt"
echo "  $TLS_DIR/server.key"
echo "  $TLS_DIR/ca.pem"
echo ""
echo "Next: restart Postgres in core-banking-system (docker compose up -d --force-recreate)"
echo "Middleware picks up ca.pem automatically when connecting to MIDDLEWARE_DB_URL."
