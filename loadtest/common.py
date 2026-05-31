"""
Shared configuration and helpers for the load/performance tests.

Two systems are involved:
  - Core Banking (Spring Boot)  — used by seed_accounts.py to create test data.
  - Middleware (FastAPI)        — the system under test in locustfile.py.

Endpoints can be hit directly on loopback (no TLS) for isolating app/DB cost,
or through Caddy on the *.local hostnames (mTLS) for a realistic measurement.

Environment variables (all optional; sane local defaults):
  MW_HOST            middleware base URL    (default http://127.0.0.1:8000)
  CB_HOST            core banking base URL  (default http://127.0.0.1:8080)
  ACCOUNTS_FILE      seeded accounts JSON   (default loadtest/accounts.json)

  # mTLS (only needed when MW_HOST / CB_HOST is an https://*.local URL)
  ATM_TLS_DIR        TLS material dir       (default ~/atm-tls)
  MTLS_KIOSK_CERT    kiosk client cert pem  (default $ATM_TLS_DIR/atm-kiosk-client.pem)
  MTLS_KIOSK_KEY     kiosk client key       (default $ATM_TLS_DIR/atm-kiosk-client.key)
  MTLS_CA_FILE       CA bundle for verify   (default $ATM_TLS_DIR/mkcert-rootCA.pem)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent

MW_HOST = os.environ.get("MW_HOST", "http://127.0.0.1:8000").rstrip("/")
CB_HOST = os.environ.get("CB_HOST", "http://127.0.0.1:8080").rstrip("/")
ACCOUNTS_FILE = Path(os.environ.get("ACCOUNTS_FILE", str(HERE / "accounts.json")))
SESSIONS_FILE = Path(os.environ.get("SESSIONS_FILE", str(HERE / "sessions.json")))


def _tls_dir() -> Path:
    raw = os.environ.get("ATM_TLS_DIR", "").strip()
    return Path(raw) if raw else Path.home() / "atm-tls"


def is_https(url: str) -> bool:
    return urlparse(url).scheme == "https"


def mtls_kwargs(url: str) -> dict:
    """
    Requests kwargs (cert + verify) for an https://*.local target behind Caddy.
    Returns {} for plain-http loopback targets so the same code path works for
    both "isolated" and "realistic" runs.
    """
    if not is_https(url):
        return {}
    tls = _tls_dir()
    cert = os.environ.get("MTLS_KIOSK_CERT", str(tls / "atm-kiosk-client.pem"))
    key = os.environ.get("MTLS_KIOSK_KEY", str(tls / "atm-kiosk-client.key"))
    ca = os.environ.get("MTLS_CA_FILE", str(tls / "mkcert-rootCA.pem"))
    kwargs: dict = {}
    if Path(cert).is_file() and Path(key).is_file():
        kwargs["cert"] = (cert, key)
    kwargs["verify"] = ca if Path(ca).is_file() else True
    return kwargs


def load_accounts() -> list[dict]:
    """Load the seeded test accounts written by seed_accounts.py."""
    if not ACCOUNTS_FILE.is_file():
        raise SystemExit(
            f"No seeded accounts at {ACCOUNTS_FILE}.\n"
            f"Run: python loadtest/seed_accounts.py --count 50"
        )
    data = json.loads(ACCOUNTS_FILE.read_text())
    if not data:
        raise SystemExit(f"{ACCOUNTS_FILE} is empty — seed accounts first.")
    return data


def save_accounts(accounts: list[dict]) -> None:
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))


def load_sessions() -> list[dict]:
    """
    Load pre-warmed session tokens written by warm_sessions.py.

    Core Banking rate-limits /atm/login (10/min per IP) to stop PIN brute force.
    Since every middleware->CB call shares one IP, logging in many users at once
    during a load run gets throttled (429 -> middleware 502). So we log in a few
    accounts slowly up front and reuse their sessions here; deposit/withdraw/
    balance are NOT rate-limited.
    """
    if not SESSIONS_FILE.is_file():
        raise SystemExit(
            f"No warmed sessions at {SESSIONS_FILE}.\n"
            f"Run: python loadtest/warm_sessions.py --count 15"
        )
    data = json.loads(SESSIONS_FILE.read_text())
    if not data:
        raise SystemExit(f"{SESSIONS_FILE} is empty — run warm_sessions.py first.")
    return data


def save_sessions(sessions: list[dict]) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))
