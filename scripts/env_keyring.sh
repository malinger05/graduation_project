#!/usr/bin/env bash
# Use a file keyring when macOS Keychain blocks/hangs Python (common in Terminal).
# Usage: source scripts/env_keyring.sh
export PYTHON_KEYRING_BACKEND="${PYTHON_KEYRING_BACKEND:-keyrings.alt.file.PlaintextKeyring}"
export KEYRING_FILE="${KEYRING_FILE:-$HOME/.atm-app-keyring}"
