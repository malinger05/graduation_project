"""Unit tests for db.py."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _restore_db_globals():
    import db

    old_engine = db._engine
    old_session_local = db._SessionLocal
    yield
    db._engine = old_engine
    db._SessionLocal = old_session_local


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

    def test_postgres_ssl_connect_args_uses_explicit_root(self, tmp_path, monkeypatch):
        import db

        ca = tmp_path / "custom-ca.pem"
        ca.write_text("fake-ca", encoding="utf-8")
        monkeypatch.setenv("POSTGRES_SSL_ROOT", str(ca))
        assert db._postgres_ssl_connect_args() == {
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        }

    def test_build_engine_returns_none_when_url_empty(self, monkeypatch):
        import db

        monkeypatch.setattr(db, "get_db_url", lambda: "")
        assert db._build_engine() is None

    def test_build_engine_passes_connect_args_when_present(self, monkeypatch):
        import db

        captured: dict[str, object] = {}

        def fake_create_engine(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return "engine"

        monkeypatch.setattr(db, "get_db_url", lambda: "postgresql://db")
        monkeypatch.setattr(
            db,
            "_postgres_ssl_connect_args",
            lambda: {"sslmode": "verify-full", "sslrootcert": "/tmp/ca.pem"},
        )
        monkeypatch.setattr(db, "create_engine", fake_create_engine)

        assert db._build_engine() == "engine"
        assert captured["url"] == "postgresql://db"
        assert captured["kwargs"] == {
            "pool_pre_ping": True,
            "future": True,
            "connect_args": {"sslmode": "verify-full", "sslrootcert": "/tmp/ca.pem"},
        }

    def test_build_engine_omits_connect_args_when_empty(self, monkeypatch):
        import db

        captured: dict[str, object] = {}

        def fake_create_engine(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return "engine-no-ssl"

        monkeypatch.setattr(db, "get_db_url", lambda: "postgresql://db")
        monkeypatch.setattr(db, "_postgres_ssl_connect_args", lambda: {})
        monkeypatch.setattr(db, "create_engine", fake_create_engine)

        assert db._build_engine() == "engine-no-ssl"
        assert captured["url"] == "postgresql://db"
        assert captured["kwargs"] == {"pool_pre_ping": True, "future": True}

    def test_init_db_returns_false_when_engine_not_built(self, monkeypatch):
        import db

        monkeypatch.setattr(db, "_build_engine", lambda: None)
        assert db.init_db() is False

    def test_init_db_initializes_and_runs_migrations(self, monkeypatch):
        import db

        class DummyMetadata:
            def __init__(self):
                self.binds = []

            def create_all(self, bind):
                self.binds.append(bind)

        class DummyBase:
            metadata = DummyMetadata()

        fake_engine = object()
        calls = {"lockouts": 0, "sessions": 0, "logs": 0}

        monkeypatch.setattr(db, "_build_engine", lambda: fake_engine)
        monkeypatch.setattr(db, "Base", DummyBase)
        monkeypatch.setattr(db, "_migrate_login_lockouts", lambda engine: calls.__setitem__("lockouts", calls["lockouts"] + 1))
        monkeypatch.setattr(db, "_migrate_session_state", lambda engine: calls.__setitem__("sessions", calls["sessions"] + 1))
        monkeypatch.setattr(
            db,
            "_migrate_transaction_logs_client_cert",
            lambda engine: calls.__setitem__("logs", calls["logs"] + 1),
        )

        def fake_sessionmaker(**kwargs):
            assert kwargs["bind"] is fake_engine
            assert kwargs["autoflush"] is False
            assert kwargs["expire_on_commit"] is False
            return "factory"

        monkeypatch.setattr(db, "sessionmaker", fake_sessionmaker)

        assert db.init_db() is True
        assert DummyBase.metadata.binds == [fake_engine]
        assert calls == {"lockouts": 1, "sessions": 1, "logs": 1}
        assert db._SessionLocal == "factory"

    def test_db_session_commits_and_closes(self, monkeypatch):
        import db

        class DummySession:
            def __init__(self):
                self.committed = False
                self.closed = False
                self.rolled_back = False

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        holder = {"session": None}

        def make_session():
            s = DummySession()
            holder["session"] = s
            return s

        monkeypatch.setattr(db, "_SessionLocal", make_session, raising=False)

        with db.db_session() as _:
            pass

        assert holder["session"].committed is True
        assert holder["session"].rolled_back is False
        assert holder["session"].closed is True

    def test_db_session_rolls_back_on_error(self, monkeypatch):
        import db

        class DummySession:
            def __init__(self):
                self.committed = False
                self.closed = False
                self.rolled_back = False

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        holder = {"session": None}

        def make_session():
            s = DummySession()
            holder["session"] = s
            return s

        monkeypatch.setattr(db, "_SessionLocal", make_session, raising=False)

        with pytest.raises(ValueError, match="boom"):
            with db.db_session():
                raise ValueError("boom")

        assert holder["session"].committed is False
        assert holder["session"].rolled_back is True
        assert holder["session"].closed is True
