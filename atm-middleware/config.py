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

# When true (default), middleware refuses to start without MIDDLEWARE_DB_URL.
# Set MIDDLEWARE_REQUIRE_DB=0 only for local experiments without Postgres.
MIDDLEWARE_REQUIRE_DB = _str("MIDDLEWARE_REQUIRE_DB", "1").lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# SQLAlchemy pool (middleware Postgres only)
MW_DB_POOL_SIZE = _int("MW_DB_POOL_SIZE", 5)
MW_DB_POOL_MAX_OVERFLOW = _int("MW_DB_POOL_MAX_OVERFLOW", 10)
MW_DB_POOL_RECYCLE_SECONDS = _int("MW_DB_POOL_RECYCLE_SECONDS", 1800)

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
    return [1.0, 1.0]


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
WORKER_FAILED_ALERT_INTERVAL_SECONDS = _float("WORKER_FAILED_ALERT_INTERVAL_SECONDS", 120)
WORKER_FAILED_ALERT_BATCH_SIZE  = _int("WORKER_FAILED_ALERT_BATCH_SIZE", 50)

# ── Retention (middleware DB) ────────────────────────────────────────────────

# Delete transaction_logs rows older than this many days. Set to 0 to disable.
TRANSACTION_LOG_RETENTION_DAYS = _int("TRANSACTION_LOG_RETENTION_DAYS", 90)

# How often the background retention job runs (default: every hour).
RETENTION_CLEANUP_INTERVAL_SECONDS = _int("RETENTION_CLEANUP_INTERVAL_SECONDS", 3600)

# ── mTLS client cert monitoring (mw.local via Caddy) ─────────────────────────

# Comma-separated serials; empty = auto-load from ~/atm-tls/*-client.pem
CLIENT_CERT_ALLOWED_SERIALS = _str("CLIENT_CERT_ALLOWED_SERIALS", "")

CLIENT_CERT_MONITOR_ENABLED = _str("CLIENT_CERT_MONITOR_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

CLIENT_CERT_MONITOR_INTERVAL_SECONDS = _int("CLIENT_CERT_MONITOR_INTERVAL_SECONDS", 300)

CLIENT_CERT_MONITOR_LOOKBACK_SECONDS = _int("CLIENT_CERT_MONITOR_LOOKBACK_SECONDS", 3600)

# Reject /atm/* when X-Client-Cert-Serial is present but not on the allow-list (default on).
CLIENT_CERT_ENFORCE_ALLOWLIST = _str("CLIENT_CERT_ENFORCE_ALLOWLIST", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# ── Fraud detection ──────────────────────────────────────────────────────────

FRAUD_DETECTION_ENABLED = _str("FRAUD_DETECTION_ENABLED", "1").strip().lower() not in (
    "0", "false", "no",
)

# Cold-start guard: behavioural checks stay OFF until the account has at least
# this many completed transactions. A first / large transaction is never flagged
# as suspicious just because there is no baseline yet.
FRAUD_MIN_HISTORY_FOR_ANOMALY = _int("FRAUD_MIN_HISTORY_FOR_ANOMALY", 5)
# Minimum same-direction samples before the relative-amount check can fire.
FRAUD_ANOMALY_MIN_SAMPLES = _int("FRAUD_ANOMALY_MIN_SAMPLES", 3)

# Universal velocity / limit controls (apply to every account, new or not).
FRAUD_VELOCITY_MAX_TXNS_PER_HOUR = _int("FRAUD_VELOCITY_MAX_TXNS_PER_HOUR", 10)
FRAUD_DAILY_WITHDRAWAL_LIMIT     = _float("FRAUD_DAILY_WITHDRAWAL_LIMIT", 2000.0)
FRAUD_LARGE_CASH_THRESHOLD       = _float("FRAUD_LARGE_CASH_THRESHOLD", 10000.0)

# Behavioural thresholds (gated by the cold-start guard above).
FRAUD_ANOMALY_MULTIPLIER = _float("FRAUD_ANOMALY_MULTIPLIER", 3.0)
FRAUD_DORMANCY_DAYS      = _int("FRAUD_DORMANCY_DAYS", 90)

# Overnight window (UTC hours). A single night transaction is just a breadcrumb;
# repeated overnight activity is reviewed (behavioural, gated).
FRAUD_NIGHT_START_HOUR = _int("FRAUD_NIGHT_START_HOUR", 0)
FRAUD_NIGHT_END_HOUR   = _int("FRAUD_NIGHT_END_HOUR", 5)
FRAUD_NIGHT_MAX_TXNS   = _int("FRAUD_NIGHT_MAX_TXNS", 3)

# Dispense-reversal abuse (universal — abnormal even for a brand-new account).
FRAUD_MAX_REVERSALS_24H = _int("FRAUD_MAX_REVERSALS_24H", 2)

# Login brute-force spread across many cards from one terminal (cert serial).
FRAUD_LOGIN_MAX_FAILS_PER_SOURCE_15M    = _int("FRAUD_LOGIN_MAX_FAILS_PER_SOURCE_15M", 15)
FRAUD_LOGIN_MAX_ACCOUNTS_PER_SOURCE_15M = _int("FRAUD_LOGIN_MAX_ACCOUNTS_PER_SOURCE_15M", 5)