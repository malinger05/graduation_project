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
