"""
db.py  —  Middleware's own database connection.

What lives in this database:
  - Operational state owned by the middleware (idempotency records, session
    state, ACK queue, etc.).

What does NOT live in this database:
  - Banking data. Accounts, balances, and transactions still live in Core
    Banking. The middleware never duplicates or computes banking state.

Connection:
  - URL is resolved via config.MIDDLEWARE_DB_URL (keychain first, .env fallback).
  - If unset, the engine is None and idempotency / sessions fall back to
    in-memory behaviour.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

Base = declarative_base()

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_db_url() -> str:
    """Resolve MIDDLEWARE_DB_URL (keychain first, then environment)."""
    import config
    return config.MIDDLEWARE_DB_URL


def _postgres_ssl_connect_args() -> dict[str, Any]:
    """
  TLS to local Docker Postgres when ~/atm-tls/postgres/ca.pem exists.
  Uses verify-full (hostname localhost must match server cert SAN).
  """
    explicit = os.environ.get("POSTGRES_SSL_ROOT", "").strip()
    if explicit:
        ca = Path(explicit)
    else:
        tls_root = os.environ.get("ATM_TLS_DIR", "").strip() or str(Path.home() / "atm-tls")
        ca = Path(tls_root) / "postgres" / "ca.pem"
    if not ca.is_file():
        return {}
    return {"sslmode": "verify-full", "sslrootcert": str(ca)}


def is_enabled() -> bool:
    """True iff a middleware database is configured and initialized."""
    return _engine is not None


def _build_engine() -> Engine | None:
    url = get_db_url()
    if not url:
        return None
    connect_args = _postgres_ssl_connect_args()
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "future": True}
    if connect_args:
        kwargs["connect_args"] = connect_args
    return create_engine(url, **kwargs)


def init_db() -> bool:
    """
    Connect to the middleware DB and create any missing tables. Returns True
    on success, False if MIDDLEWARE_DB_URL is unset. Raises on connection
    failure so misconfiguration is surfaced loudly at startup.
    """
    global _engine, _SessionLocal

    _engine = _build_engine()
    if _engine is None:
        return False

    # Import models so SQLAlchemy registers them on Base.metadata BEFORE
    # create_all runs. Keep this import local to avoid a circular import at
    # module load time.
    import models  # noqa: F401

    Base.metadata.create_all(bind=_engine)
    _migrate_login_lockouts(_engine)
    _migrate_session_state(_engine)
    _migrate_transaction_logs_client_cert(_engine)
    _migrate_transaction_logs_fraud(_engine)
    
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return True


def _migrate_login_lockouts(engine: Engine) -> None:
    """Add lock_tier / permanently_locked on existing deployments."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE login_lockouts "
                "ADD COLUMN IF NOT EXISTS lock_tier INTEGER NOT NULL DEFAULT 0"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE login_lockouts "
                "ADD COLUMN IF NOT EXISTS permanently_locked BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE login_lockouts "
                "ADD COLUMN IF NOT EXISTS must_reset_pin BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )


def _migrate_session_state(engine: Engine) -> None:
    """Add card_number column to session_state on existing deployments."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE session_state "
                "ADD COLUMN IF NOT EXISTS card_number VARCHAR NOT NULL DEFAULT ''"
            )
        )


def _migrate_transaction_logs_client_cert(engine: Engine) -> None:
    """Add mTLS client cert columns to transaction_logs on existing deployments."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE transaction_logs "
                "ADD COLUMN IF NOT EXISTS client_cert_subject VARCHAR"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE transaction_logs "
                "ADD COLUMN IF NOT EXISTS client_cert_serial VARCHAR"
            )
        )


def _migrate_transaction_logs_fraud(engine: Engine) -> None:
    """Add fraud_signals column to transaction_logs on existing deployments."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE transaction_logs "
                "ADD COLUMN IF NOT EXISTS fraud_signals JSONB"
            )
        )


@contextmanager
def db_session() -> Iterator[Session]:
    """Context-managed SQLAlchemy session. Commits on success, rolls back on error."""
    if _SessionLocal is None:
        raise RuntimeError(
            "Middleware DB is not initialized. Set MIDDLEWARE_DB_URL in keychain "
            "and call init_db()."
        )
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()