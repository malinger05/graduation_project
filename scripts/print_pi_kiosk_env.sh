#!/usr/bin/env bash
# Run on the LAPTOP (where middleware runs) to print Pi .env lines.
# Usage: scripts/print_pi_kiosk_env.sh
set -euo pipefail

pick_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    local ts
    ts="$(tailscale ip -4 2>/dev/null || true)"
    if [[ -n "$ts" ]]; then
      echo "$ts"
      return
    fi
  fi
  hostname -I 2>/dev/null | awk '{print $1}'
}

IP="$(pick_ip)"
if [[ -z "$IP" ]]; then
  echo "Could not detect LAN/Tailscale IP. Set MIDDLEWARE_URL manually on the Pi." >&2
  exit 1
fi

cat <<EOF
# Copy these lines to the Raspberry Pi .env (graduation_project/.env)
# Use the LAPTOP IP below — NOT the Pi's own IP.

MIDDLEWARE_URL=http://${IP}:8000
MTLS_DISABLE=1
BIND_HOST=0.0.0.0
PORT=5001

# Fingerprint sensor on the Pi:
# FINGERPRINT_PORT=/dev/ttyUSB0
# FINGERPRINT_SIMULATE=1
EOF
