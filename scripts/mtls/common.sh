#!/usr/bin/env bash
# Shared mkcert client-cert issuance for kiosk and middleware mTLS.
set -euo pipefail

mtls_tls_dir() {
  echo "${ATM_TLS_DIR:-$HOME/atm-tls}"
}

mtls_require_mkcert() {
  if ! command -v mkcert >/dev/null 2>&1; then
    echo "mkcert is required. Install: brew install mkcert && mkcert -install" >&2
    exit 1
  fi
  local caroot ca_cert ca_key
  caroot="$(mkcert -CAROOT)"
  ca_cert="$caroot/rootCA.pem"
  ca_key="$caroot/rootCA-key.pem"
  if [[ ! -f "$ca_cert" || ! -f "$ca_key" ]]; then
    echo "mkcert CA not found. Run: mkcert -install" >&2
    exit 1
  fi
  echo "$ca_cert|$ca_key"
}

mtls_sync_ca_copy() {
  local tls_dir="$1" ca_cert="$2"
  cp "$ca_cert" "$tls_dir/mkcert-rootCA.pem"
}

# Issue or re-issue a client cert at fixed paths (atomic install).
# Usage: mtls_issue_client_cert <CN> <key.pem> <cert.pem> [validity_days] [new_key:0|1]
mtls_issue_client_cert() {
  local cn="$1" key="$2" cert="$3"
  local validity_days="${4:-${MTLS_CERT_VALIDITY_DAYS:-90}}"
  local new_key="${5:-1}"

  local pair ca_cert ca_key tls_dir
  pair="$(mtls_require_mkcert)"
  ca_cert="${pair%%|*}"
  ca_key="${pair#*|}"
  tls_dir="$(dirname "$cert")"
  mkdir -p "$tls_dir"
  mtls_sync_ca_copy "$tls_dir" "$ca_cert"

  local tmp_dir csr tmp_key tmp_cert
  tmp_dir="$(mktemp -d "${tls_dir}/.mtls-rotate.XXXXXX")"
  trap 'rm -rf "$tmp_dir"' RETURN
  csr="$tmp_dir/client.csr"
  tmp_key="$tmp_dir/client.key"
  tmp_cert="$tmp_dir/client.pem"

  if [[ "$new_key" == "1" || ! -f "$key" ]]; then
    openssl genrsa -out "$tmp_key" 2048
  else
    cp "$key" "$tmp_key"
    chmod 600 "$tmp_key"
  fi

  openssl req -new -key "$tmp_key" -out "$csr" -subj "/CN=${cn}"
  openssl x509 -req -in "$csr" -CA "$ca_cert" -CAkey "$ca_key" \
    -CAcreateserial -out "$tmp_cert" -days "$validity_days" -sha256

  chmod 600 "$tmp_key"
  install -m 600 "$tmp_key" "${key}.new"
  install -m 644 "$tmp_cert" "${cert}.new"
  mv -f "${key}.new" "$key"
  mv -f "${cert}.new" "$cert"
}
