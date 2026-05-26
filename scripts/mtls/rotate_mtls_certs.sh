#!/usr/bin/env bash
# Renew kiosk and middleware mTLS client certs before they expire (mkcert CA).
# mTLS stays enabled; apps read the same paths on each HTTP call — no restart required.
#
# Usage:
#   ./scripts/mtls/rotate_mtls_certs.sh           # renew only when within window
#   ./scripts/mtls/rotate_mtls_certs.sh --force   # renew now
#   MTLS_CERT_VALIDITY_DAYS=1 ./scripts/mtls/rotate_mtls_certs.sh --force  # quick demo
#
# Optional cron (daily):
#   0 3 * * * cd /path/to/graduation_project && ./scripts/mtls/rotate_mtls_certs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=scripts/mtls/common.sh
source "$ROOT/scripts/mtls/common.sh"

FORCE=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

cd "$ROOT"

TLS_DIR="$(mtls_tls_dir)"
RENEW_BEFORE="${MTLS_RENEW_BEFORE_DAYS:-30}"
VALIDITY="${MTLS_CERT_VALIDITY_DAYS:-90}"
ROTATE_KEYS="${MTLS_ROTATE_KEYS:-1}"

KIOSK_CERT="$TLS_DIR/atm-kiosk-client.pem"
KIOSK_KEY="$TLS_DIR/atm-kiosk-client.key"
ADMIN_CERT="$TLS_DIR/admin-staff-client.pem"
ADMIN_KEY="$TLS_DIR/admin-staff-client.key"
MW_CERT="$TLS_DIR/middleware-client.pem"
MW_KEY="$TLS_DIR/middleware-client.key"

should_rotate() {
  local cert="$1"
  if [[ "$FORCE" == "1" ]]; then
    return 0
  fi
  python3 - "$cert" "$RENEW_BEFORE" <<'PY'
import sys
from pathlib import Path
from mtls_cert import needs_renewal

cert = Path(sys.argv[1])
renew = float(sys.argv[2])
sys.exit(0 if needs_renewal(cert, renew) else 1)
PY
}

rotate_one() {
  local label="$1" cn="$2" key="$3" cert="$4"
  if ! should_rotate "$cert"; then
    local days
    days="$(python3 - "$cert" <<'PY'
import sys
from pathlib import Path
from mtls_cert import days_until_expiry

d = days_until_expiry(Path(sys.argv[1]))
print(f"{d:.1f}" if d is not None else "?")
PY
)"
    echo "[$label] OK — expires in ${days} days (renew when <= ${RENEW_BEFORE})"
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[$label] would renew (<= ${RENEW_BEFORE} days left or --force)"
    return 0
  fi
  echo "[$label] renewing client cert (validity ${VALIDITY} days)..."
  mtls_issue_client_cert "$cn" "$key" "$cert" "$VALIDITY" "$ROTATE_KEYS"
  local days
  days="$(python3 - "$cert" <<'PY'
import sys
from pathlib import Path
from mtls_cert import days_until_expiry

d = days_until_expiry(Path(sys.argv[1]))
print(f"{d:.1f}" if d is not None else "?")
PY
)"
  echo "[$label] renewed — expires in ${days} days"
}

rotate_one "kiosk" "atm-kiosk" "$KIOSK_KEY" "$KIOSK_CERT"
rotate_one "admin" "admin-staff" "$ADMIN_KEY" "$ADMIN_CERT"
rotate_one "middleware" "middleware" "$MW_KEY" "$MW_CERT"
echo "Done. mTLS paths unchanged; Caddy still trusts mkcert CA."
