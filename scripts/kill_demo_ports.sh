#!/usr/bin/env bash
# Stop leftover demo processes (zombie uvicorn can block 127.0.0.1:8000 on macOS).
# Sends SIGTERM first and waits before SIGKILL.
set -euo pipefail

_graceful_kill() {
  local pattern="$1"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0
  kill $pids 2>/dev/null || true
  sleep 3
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
}

_graceful_kill 'atm-middleware/middleware'
_graceful_kill 'uvicorn middleware:app'
_graceful_kill 'python.*middleware\.py'
_graceful_kill 'customer_app\.py'
_graceful_kill 'admin_app\.py'
_graceful_kill 'spring-boot:run'

for port in 8000 8080 5001 5002 443; do
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -z "$pids" ]] && continue
  kill $pids 2>/dev/null || true
  sleep 2
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
done
echo "Demo ports cleared (8000, 8080, 5001, 5002, 443)."
