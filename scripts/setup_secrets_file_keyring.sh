#!/usr/bin/env bash
# One-shot: install file keyring + load secrets from .env.save (avoids hung macOS Keychain UI).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT/atm_venv}"
PY="${VENV_DIR}/bin/python"
ENV_FILE="${1:-$ROOT/.env.save}"
export KEYRING_FILE="${KEYRING_FILE:-$HOME/.atm-app-keyring}"
export PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring

"$PY" -m pip install -q keyrings.alt
"$PY" <<PY
import os, secrets
from pathlib import Path
os.environ["PYTHON_KEYRING_BACKEND"] = "keyrings.alt.file.PlaintextKeyring"
os.environ["KEYRING_FILE"] = os.path.expanduser("${KEYRING_FILE}")
import keyring

def load(path):
    d = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip()
    return d

env = load("${ENV_FILE}")
keyring.set_password("atm-app", "FLASK_SECRET_KEY", secrets.token_urlsafe(48))
keyring.set_password("atm-app", "MIDDLEWARE_DB_URL", env.get("MIDDLEWARE_DB_URL", "postgresql+psycopg://mwuser:mwpass@localhost:5433/mwuser"))
if env.get("CONTRACT_ADDRESS"):
    keyring.set_password("atm-app", "CONTRACT_ADDRESS", env["CONTRACT_ADDRESS"])
if env.get("ETH_PRIVATE_KEY"):
    keyring.set_password("atm-app", "ETH_PRIVATE_KEY", env["ETH_PRIVATE_KEY"])
keyring.set_password("atm-app", "MIDDLEWARE_SERVICE_TOKEN", env.get("MIDDLEWARE_SERVICE_TOKEN", "dev-only-change-me"))
print("Secrets stored in", os.environ["KEYRING_FILE"])
print("Before run_demo:  source scripts/env_keyring.sh")
PY
