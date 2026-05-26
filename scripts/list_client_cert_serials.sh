#!/usr/bin/env bash
# Print allowed mTLS client certificate serials (for CLIENT_CERT_ALLOWED_SERIALS / monitoring).
set -euo pipefail

TLS_DIR="${ATM_TLS_DIR:-$HOME/atm-tls}"

for name in atm-kiosk-client admin-staff-client middleware-client; do
  pem="$TLS_DIR/${name}.pem"
  if [[ ! -f "$pem" ]]; then
    echo "$name: (missing $pem)"
    continue
  fi
  serial="$(openssl x509 -in "$pem" -noout -serial 2>/dev/null | sed 's/^serial=//')"
  subj="$(openssl x509 -in "$pem" -noout -subject 2>/dev/null | sed 's/^subject=//')"
  echo "$name: serial=$serial"
  echo "         subject=$subj"
done

echo ""
echo "Re-install Caddyfile after cert changes: ./scripts/caddy/install_caddyfile.sh"
