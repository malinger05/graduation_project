"""Unit tests for sessions.py — in-memory session storage."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException


def _session_create_kwargs(**overrides):
    """Defaults aligned with sessions.create() after card-based login."""
    base = {
        "jwt": "jwt-test",
        "account_id": 1,
        "account_number": "ACC001",
        "card_number": "4111111111111111",
        "balance": 0.0,
        "customer_name": "Test User",
    }
    base.update(overrides)
    return base


@pytest.mark.memory
class TestSessionCreate:
    def test_create_returns_opaque_token(self, sessions_short_ttl):
        token = sessions_short_ttl.create(**_session_create_kwargs(balance=500.0))
        assert isinstance(token, str)
        assert len(token) == 64

    def test_create_then_get_returns_session_fields(self, sessions_short_ttl):
        s = sessions_short_ttl
        token = s.create(
            **_session_create_kwargs(
                jwt="jwt-x",
                account_id=42,
                account_number="ACC42",
                card_number="5500000000000004",
                balance=1000.0,
                customer_name="Alice",
            )
        )
        data = s.get(token)
        assert data["jwt"] == "jwt-x"
        assert data["account_id"] == 42
        assert data["account_number"] == "ACC42"
        assert data["card_number"] == "5500000000000004"
        assert data["balance"] == 1000.0
        assert data["customer_name"] == "Alice"


@pytest.mark.memory
class TestSessionGet:
    def test_get_invalid_token_raises_401(self, sessions_short_ttl):
        with pytest.raises(HTTPException) as exc:
            sessions_short_ttl.get("not-a-real-token")
        assert exc.value.status_code == 401

    def test_get_expired_session_raises_401(self, sessions_short_ttl):
        s = sessions_short_ttl
        s.configure(1)
        token = s.create(**_session_create_kwargs())
        with patch("time.time", return_value=time.time() + 10):
            with pytest.raises(HTTPException) as exc:
                s.get(token)
        assert exc.value.status_code == 401
        s.configure(60)


@pytest.mark.memory
class TestSessionUpdateAndTouch:
    def test_update_balance_persisted_on_get(self, sessions_short_ttl):
        s = sessions_short_ttl
        token = s.create(**_session_create_kwargs(balance=100.0))
        s.update_balance(token, 250.5)
        assert s.get(token)["balance"] == 250.5

    def test_touch_valid_session_returns_true(self, sessions_short_ttl):
        s = sessions_short_ttl
        token = s.create(**_session_create_kwargs())
        assert s.touch(token) is True

    def test_touch_empty_token_returns_false(self, sessions_short_ttl):
        assert sessions_short_ttl.touch("") is False

    def test_touch_unknown_token_returns_false(self, sessions_short_ttl):
        assert sessions_short_ttl.touch("deadbeef" * 4) is False

    def test_touch_expired_returns_false(self, sessions_short_ttl):
        s = sessions_short_ttl
        s.configure(1)
        token = s.create(**_session_create_kwargs())
        with patch("time.time", return_value=time.time() + 5):
            assert s.touch(token) is False
        s.configure(60)


@pytest.mark.memory
class TestSessionRemoveAndCleanup:
    def test_remove_then_get_fails(self, sessions_short_ttl):
        s = sessions_short_ttl
        token = s.create(**_session_create_kwargs())
        s.remove(token)
        with pytest.raises(HTTPException):
            s.get(token)

    def test_cleanup_expired_removes_stale_sessions(self, sessions_short_ttl):
        s = sessions_short_ttl
        s.configure(1)
        token = s.create(**_session_create_kwargs())
        with patch("time.time", return_value=time.time() + 5):
            removed = s.cleanup_expired()
        assert removed >= 1
        with pytest.raises(HTTPException):
            s.get(token)
        s.configure(60)

    def test_cleanup_keeps_active_session(self, sessions_short_ttl):
        s = sessions_short_ttl
        token = s.create(**_session_create_kwargs(account_number="A"))
        removed = s.cleanup_expired()
        assert removed == 0
        assert s.get(token)["account_number"] == "A"

    def test_get_refreshes_last_active(self, sessions_short_ttl):
        s = sessions_short_ttl
        s.configure(2)
        token = s.create(**_session_create_kwargs())
        s.get(token)
        with patch("time.time", return_value=time.time() + 1.5):
            assert s.get(token)["account_number"] == "ACC001"
        s.configure(60)


@pytest.mark.memory
@pytest.mark.db
class TestSessionDbPath:
    def test_create_persists_card_number(self, middleware_db, sessions_short_ttl):
        import db
        from models import SessionState

        token = sessions_short_ttl.create(
            **_session_create_kwargs(card_number="4000000000000002")
        )
        with db.db_session() as s:
            row = s.get(SessionState, token)
        assert row is not None
        assert row.card_number == "4000000000000002"
