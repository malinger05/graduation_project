"""
Shared fixtures for atm-middleware unit tests.

Adds atm-middleware/ to sys.path and resets in-memory module state between tests.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_MW = Path(__file__).resolve().parents[2] / "atm-middleware"
if str(_MW) not in sys.path:
    sys.path.insert(0, str(_MW))


@pytest.fixture(autouse=True)
def _reset_memory_stores():
    """Clear in-memory lockout/session dicts before each test."""
    import lockouts
    import sessions

    with lockouts._memory_lock:
        lockouts._memory.clear()
    with sessions._memory_lock:
        sessions._memory.clear()
    yield
    with lockouts._memory_lock:
        lockouts._memory.clear()
    with sessions._memory_lock:
        sessions._memory.clear()


@pytest.fixture
def lockouts_fast():
    """Three failures per block; sub-second lock tiers for time-based tests."""
    import lockouts

    lockouts.configure(3, [0.001, 0.002])
    yield lockouts
    lockouts.configure(3, [15, 30])


@pytest.fixture
def lockouts_standard():
    import lockouts

    lockouts.configure(3, [15, 30])
    yield lockouts


@pytest.fixture
def sessions_short_ttl():
    import sessions

    sessions.configure(60)
    yield sessions
    sessions.configure(900)


@pytest.fixture
def utc_now():
    """Fixed clock for lockout/session expiry tests."""
    return datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def middleware_db(monkeypatch):
    """
    In-memory SQLite middleware DB (JSONB columns swapped to JSON).
    Patches db._engine / _SessionLocal so is_enabled() is True.
    """
    import db
    import models

    for table in (
        models.IdempotencyRecord,
        models.TransactionLog,
        models.CorrelationLog,
    ):
        for col in table.__table__.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()

    # StaticPool keeps one shared in-memory SQLite DB across connections.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import models  # noqa: F401 — register tables on Base.metadata

    db.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setattr(db, "_engine", engine, raising=False)
    monkeypatch.setattr(db, "_SessionLocal", factory, raising=False)
    monkeypatch.setattr(db, "get_db_url", lambda: "sqlite:///:memory:")

    assert db.is_enabled()

    yield factory

    monkeypatch.setattr(db, "_engine", None, raising=False)
    monkeypatch.setattr(db, "_SessionLocal", None, raising=False)


@pytest.fixture
def db_disabled(monkeypatch):
    import db

    monkeypatch.setattr(db, "_engine", None, raising=False)
    monkeypatch.setattr(db, "_SessionLocal", None, raising=False)
    assert not db.is_enabled()
