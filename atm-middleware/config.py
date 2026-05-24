"""
config.py  —  Middleware settings resolved keychain-first, then .env.

All values used by the middleware layer should be read through this module
so configuration is consistent. Sensitive secrets never fall back to .env
(see secrets_manager.SENSITIVE_SECRET_NAMES).
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from secrets_manager import SENSITIVE_SECRET_NAMES, get_secret  # noqa: E402


def _str(name: str, default: str = "") -> str:
    allow_env = name not in SENSITIVE_SECRET_NAMES
    return get_secret(name, default, allow_env_fallback=allow_env).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name, str(default))
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _str(name, str(default))
    return float(raw) if raw else default


# ── Database (middleware Postgres) ───────────────────────────────────────────

MIDDLEWARE_DB_URL = _str("MIDDLEWARE_DB_URL")

# ── Core Banking bridge ──────────────────────────────────────────────────────

CORE_BANKING_URL = _str("CORE_BANKING_URL", "https://api.local").rstrip("/")
SERVICE_TOKEN    = _str("MIDDLEWARE_SERVICE_TOKEN")

# mTLS client cert for https://api.local (paths under ~/atm-tls by default)
MTLS_CA_FILE = _str("MTLS_CA_FILE")

# ── Sessions / lockouts ──────────────────────────────────────────────────────

ACK_TIMEOUT_SECONDS  = _int("ACK_TIMEOUT_SECONDS", 30)
SESSION_TTL_SECONDS  = _int("SESSION_TTL_SECONDS", 900)  # 15 min server backstop
LOCKOUT_MAX_ATTEMPTS = _int("LOCKOUT_MAX_ATTEMPTS", 3)


def _lockout_minutes_list() -> list[float]:
    """
    Comma-separated minutes per tier, e.g. LOCKOUT_MINUTES=15,30
    LOCKOUT_FAST_TEST=1 → 0.1,0.15 (~6s / ~9s) for manual testing.
    """
    if _str("LOCKOUT_FAST_TEST", "").lower() in ("1", "true", "yes", "on"):
        return [0.1, 0.15]
    raw = _str("LOCKOUT_MINUTES", "")
    if raw:
        return [float(x.strip()) for x in raw.split(",") if x.strip()]
    return [15.0, 30.0]


LOCKOUT_MINUTES = _lockout_minutes_list()

# ── Blockchain ───────────────────────────────────────────────────────────────

CONTRACT_ADDRESS  = _str("CONTRACT_ADDRESS")
ETH_PRIVATE_KEY   = _str("ETH_PRIVATE_KEY")
RPC_URL           = _str("ETH_RPC_URL", "https://ethereum-sepolia.publicnode.com")
_RPC_FALLBACKS    = _str(
    "ETH_RPC_FALLBACK_URLS",
    "https://sepolia.drpc.org,https://1rpc.io/sepolia",
)
RPC_FALLBACK_URLS = [u.strip() for u in _RPC_FALLBACKS.split(",") if u.strip()]

# ── Reconciliation worker ──────────────────────────────────────────────────────

WORKER_RETRY_INTERVAL_SECONDS   = _float("WORKER_RETRY_INTERVAL_SECONDS",   30)
WORKER_CONFIRM_INTERVAL_SECONDS = _float("WORKER_CONFIRM_INTERVAL_SECONDS", 60)
WORKER_TAMPER_INTERVAL_SECONDS  = _float("WORKER_TAMPER_INTERVAL_SECONDS",  300)
WORKER_RETRY_BATCH_SIZE         = _int("WORKER_RETRY_BATCH_SIZE",   25)
WORKER_CONFIRM_BATCH_SIZE       = _int("WORKER_CONFIRM_BATCH_SIZE", 25)
WORKER_TAMPER_BATCH_SIZE        = _int("WORKER_TAMPER_BATCH_SIZE",  100)
WORKER_TAMPER_LOOKBACK_HOURS    = _int("WORKER_TAMPER_LOOKBACK_HOURS", 24)
WORKER_MAX_SUBMIT_ATTEMPTS      = _int("WORKER_MAX_SUBMIT_ATTEMPTS", 8)

# ── Retention (middleware DB) ────────────────────────────────────────────────

# Delete transaction_logs rows older than this many days. Set to 0 to disable.
TRANSACTION_LOG_RETENTION_DAYS = _int("TRANSACTION_LOG_RETENTION_DAYS", 90)

# How often the background retention job runs (default: every hour).
RETENTION_CLEANUP_INTERVAL_SECONDS = _int("RETENTION_CLEANUP_INTERVAL_SECONDS", 3600)
