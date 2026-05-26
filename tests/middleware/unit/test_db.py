"""Unit tests for db.py."""

from __future__ import annotations

import pytest


class TestDbEnabled:
    def test_is_enabled_false_by_default(self, db_disabled):
        import db

        assert not db.is_enabled()

    def test_is_enabled_true_with_fixture(self, middleware_db):
        import db

        assert db.is_enabled()

    def test_db_session_raises_when_not_initialized(self, db_disabled):
        import db

        with pytest.raises(RuntimeError, match="not initialized"):
            with db.db_session():
                pass

    def test_get_db_url_delegates_to_config(self, monkeypatch):
        import config
        import db

        monkeypatch.setattr(config, "MIDDLEWARE_DB_URL", "postgresql://testdb", raising=False)
        assert db.get_db_url() == "postgresql://testdb"

    def test_postgres_ssl_connect_args_when_ca_missing(self, monkeypatch):
        import db

        monkeypatch.delenv("POSTGRES_SSL_ROOT", raising=False)
        monkeypatch.setenv("ATM_TLS_DIR", "/nonexistent-atm-tls")
        assert db._postgres_ssl_connect_args() == {}

    def test_postgres_ssl_connect_args_when_ca_present(self, tmp_path, monkeypatch):
        import db

        ca = tmp_path / "postgres" / "ca.pem"
        ca.parent.mkdir()
        ca.write_text("fake-ca", encoding="utf-8")
        monkeypatch.setenv("ATM_TLS_DIR", str(tmp_path))
        assert db._postgres_ssl_connect_args() == {
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        }
