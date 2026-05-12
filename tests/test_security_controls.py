"""
Security-focused regression tests.

Patches ``secrets_manager.get_secret`` so ``FLASK_SECRET_KEY`` resolves without
the OS keychain. PostgreSQL and Ethereum are not required for these checks.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from secure_user_db import SecureUserDatabase


def _fake_get_secret(name, default=None, required=False, allow_env_fallback=None):
    if name == "FLASK_SECRET_KEY":
        return "test-flask-secret-key-for-pytest-only!!"
    return default if default is not None else ""


@pytest.fixture(scope="module")
def customer_web():
    """Load ``customer_app`` once with a stable Flask secret for tests."""
    with patch("secrets_manager.get_secret", side_effect=_fake_get_secret):
        import customer_app as m

        m.app.config["TESTING"] = True
        yield m


@pytest.fixture
def client(customer_web):
    with customer_web.app.test_client() as c:
        yield c


class TestWebCsrfAndValidation:
    """CSRF and whitelist validation on POST (no full ATM initialization)."""

    def test_post_login_without_csrf_redirects_and_blocks_action(self, client):
        resp = client.post(
            "/login",
            data={"account": "1001", "pin": "1234"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.location.endswith("/login")

    def test_post_login_after_get_requires_csrf_token(self, client):
        get_resp = client.get("/login")
        assert get_resp.status_code == 200
        token = _csrf_token_from_response(get_resp.data.decode())
        assert token

        bad = client.post(
            "/login",
            data={"account": "1001", "pin": "1234", "csrf_token": "wrong-token"},
            follow_redirects=False,
        )
        assert bad.status_code == 302

        ok = client.post(
            "/login",
            data={"account": "1001", "pin": "1234", "csrf_token": token},
            follow_redirects=False,
        )
        # Wrong PIN still hits ATM — without DB/chain may be 503 or redirect to dashboard.
        assert ok.status_code in (302, 503)

    def test_sqli_payload_rejected_by_format_validator(self, client):
        get_resp = client.get("/login")
        token = _csrf_token_from_response(get_resp.data.decode())
        resp = client.post(
            "/login",
            data={
                "account": "1001' OR 1=1 --",
                "pin": "1234",
                "csrf_token": token,
            },
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Invalid account format or PIN format" in body

    def test_dashboard_requires_login(self, client):
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in (resp.location or "")

    def test_security_headers_on_get_login(self, client):
        resp = client.get("/login")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"


class TestSecureUserDatabaseLockoutAndPinStorage:
    """SQLite user store: lockout + Argon2 pin hash (isolated temp files)."""

    def test_failed_attempts_trigger_lockout(self, tmp_path: Path):
        db_path = tmp_path / "users_test.db"
        key_path = tmp_path / "aes.key"
        db = SecureUserDatabase(db_path=str(db_path), key_path=str(key_path))

        wrong = db.authenticate_with_status("1001", "9999")
        assert wrong.get("status") == "invalid"

        wrong2 = db.authenticate_with_status("1001", "9999")
        assert wrong2.get("status") == "invalid"

        locked = db.authenticate_with_status("1001", "9999")
        assert locked.get("status") == "locked"
        assert locked.get("remaining_lock_seconds", 0) > 0

    def test_pin_stored_as_argon2_not_plaintext(self, tmp_path: Path):
        db_path = tmp_path / "users_test2.db"
        key_path = tmp_path / "aes2.key"
        SecureUserDatabase(db_path=str(db_path), key_path=str(key_path))

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT pin_hash FROM users WHERE account_number = ?",
                ("1001",),
            ).fetchone()
            assert row is not None
            pin_hash = row[0]
            assert pin_hash.startswith("$argon2")
            assert "1234" not in pin_hash
        finally:
            conn.close()


def _csrf_token_from_response(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    return m.group(1) if m else ""
