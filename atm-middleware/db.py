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

from contextlib import contextmanager
from typing import Iterator

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


def is_enabled() -> bool:
    """True iff a middleware database is configured and initialized."""
    return _engine is not None


def _build_engine() -> Engine | None:
    url = get_db_url()
    if not url:
        return None
    return create_engine(url, pool_pre_ping=True, future=True)


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
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return True


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
