#!/usr/bin/env bash
# Admin panel → https://mw.local mTLS client cert (separate from kiosk).
# Re-issue: ./scripts/gen_admin_client_cert.sh --force
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/mtls/common.sh
source "$ROOT/scripts/mtls/common.sh"

FORCE=0
for arg in "$@"; do
  [[ "$arg" == "--force" ]] && FORCE=1
done

TLS_DIR="$(mtls_tls_dir)"
KEY="$TLS_DIR/admin-staff-client.key"
CERT="$TLS_DIR/admin-staff-client.pem"
VALIDITY="${MTLS_CERT_VALIDITY_DAYS:-825}"

if [[ -f "$CERT" && -f "$KEY" && "$FORCE" != "1" ]]; then
  echo "Admin staff client cert already exists:"
  echo "  $CERT"
  echo "  $KEY"
  exit 0
fi

mtls_issue_client_cert "admin-staff" "$KEY" "$CERT" "$VALIDITY" 1
echo "Created admin mTLS client certificate (admin_app → mw.local):"
echo "  $CERT"
echo "  $KEY"
