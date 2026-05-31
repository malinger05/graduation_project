"""Unit tests for idempotency.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import idempotency
from models import IdempotencyRecord


@pytest.mark.db
class TestIdempotencyBeginValidation:
    def test_missing_key_raises_400(self, middleware_db):
        with pytest.raises(HTTPException) as exc:
            idempotency.begin(None, "ACC", "/atm/deposit", {"amount": 10})
        assert exc.value.status_code == 400

    def test_blank_key_raises_400(self, middleware_db):
        with pytest.raises(HTTPException) as exc:
            idempotency.begin("   ", "ACC", "/atm/deposit", {"amount": 10})
        assert exc.value.status_code == 400


@pytest.mark.db
class TestIdempotencyDisabled:
    def test_begin_noop_when_db_disabled(self, db_disabled):
        assert idempotency.begin("key-1", "ACC", "/atm/withdraw", {"amount": 5}) is None

    def test_finish_noop_when_db_disabled(self, db_disabled):
        idempotency.finish("key-1", "ACC", {"ok": True})


@pytest.mark.db
class TestIdempotencyHappyPath:
    def test_begin_claims_fresh_request(self, middleware_db):
        result = idempotency.begin("k1", "ACC1", "/atm/deposit", {"amount": 50})
        assert result is None

    def test_finish_then_begin_returns_cached(self, middleware_db):
        body = {"status": "SUCCESS", "amount": 50}
        idempotency.begin("k2", "ACC1", "/atm/deposit", {"amount": 50})
        idempotency.finish("k2", "ACC1", body)
        cached = idempotency.begin("k2", "ACC1", "/atm/deposit", {"amount": 50})
        assert cached == body

    def test_different_key_not_cached(self, middleware_db):
        idempotency.begin("ka", "ACC1", "/atm/deposit", {"amount": 1})
        idempotency.finish("ka", "ACC1", {"v": 1})
        assert idempotency.begin("kb", "ACC1", "/atm/deposit", {"amount": 1}) is None


@pytest.mark.db
class TestIdempotencyConflicts:
    def test_same_key_different_body_raises_422(self, middleware_db):
        idempotency.begin("kc", "ACC1", "/atm/withdraw", {"amount": 10})
        idempotency.finish("kc", "ACC1", {"ok": True})
        with pytest.raises(HTTPException) as exc:
            idempotency.begin("kc", "ACC1", "/atm/withdraw", {"amount": 20})
        assert exc.value.status_code == 422

    def test_in_progress_fresh_raises_409(self, middleware_db):
        idempotency.begin("kd", "ACC1", "/atm/deposit", {"amount": 5})
        with pytest.raises(HTTPException) as exc:
            idempotency.begin("kd", "ACC1", "/atm/deposit", {"amount": 5})
        assert exc.value.status_code == 409


@pytest.mark.db
class TestIdempotencyStaleAndExpiry:
    def test_stale_in_progress_allows_retry(self, middleware_db):
        import db

        now = datetime.now(timezone.utc)
        stale_created = now - timedelta(seconds=120)
        with db.db_session() as s:
            s.add(
                IdempotencyRecord(
                    account_number="ACC1",
                    idempotency_key="stale",
                    endpoint="/atm/deposit",
                    request_fingerprint="fp",
                    status="in_progress",
                    created_at=stale_created,
                    expires_at=now + timedelta(hours=24),
                )
            )
        with patch("idempotency._fingerprint", return_value="fp"):
            assert idempotency.begin("stale", "ACC1", "/atm/deposit", {"amount": 1}) is None

    def test_expired_record_treated_as_fresh(self, middleware_db):
        import db

        now = datetime.now(timezone.utc)
        with db.db_session() as s:
            s.add(
                IdempotencyRecord(
                    account_number="ACC1",
                    idempotency_key="exp",
                    endpoint="/atm/deposit",
                    request_fingerprint="old",
                    status="completed",
                    response_body={"old": True},
                    created_at=now - timedelta(hours=48),
                    expires_at=now - timedelta(hours=1),
                )
            )
        assert idempotency.begin("exp", "ACC1", "/atm/deposit", {"amount": 1}) is None


@pytest.mark.db
class TestIdempotencyFingerprint:
    def test_fingerprint_stable_for_same_input(self):
        fp1 = idempotency._fingerprint("/atm/deposit", "A", {"amount": 10})
        fp2 = idempotency._fingerprint("/atm/deposit", "A", {"amount": 10})
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_fingerprint_differs_by_endpoint(self):
        body = {"amount": 10}
        fp1 = idempotency._fingerprint("/atm/deposit", "A", body)
        fp2 = idempotency._fingerprint("/atm/withdraw", "A", body)
        assert fp1 != fp2

    def test_fingerprint_differs_by_account(self):
        fp1 = idempotency._fingerprint("/atm/deposit", "A1", {"amount": 10})
        fp2 = idempotency._fingerprint("/atm/deposit", "A2", {"amount": 10})
        assert fp1 != fp2

    def test_finish_without_begin_is_safe(self, middleware_db):
        idempotency.finish("ghost", "ACC1", {"x": 1})

    def test_abort_removes_in_progress(self, middleware_db):
        idempotency.begin("abort-me", "ACC1", "/atm/deposit", {"amount": 1})
        idempotency.abort("abort-me", "ACC1")
        assert idempotency.begin("abort-me", "ACC1", "/atm/deposit", {"amount": 1}) is None

    def test_abort_noop_when_completed(self, middleware_db):
        idempotency.begin("done", "ACC1", "/atm/deposit", {"amount": 1})
        idempotency.finish("done", "ACC1", {"ok": True})
        idempotency.abort("done", "ACC1")
        cached = idempotency.begin("done", "ACC1", "/atm/deposit", {"amount": 1})
        assert cached == {"ok": True}

    def test_accounts_isolated_same_key(self, middleware_db):
        idempotency.begin("shared", "ACC-A", "/atm/deposit", {"amount": 1})
        idempotency.finish("shared", "ACC-A", {"from": "A"})
        assert idempotency.begin("shared", "ACC-B", "/atm/deposit", {"amount": 1}) is None
