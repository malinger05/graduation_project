"""Unit tests for cb_http.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


class TestPathHelpers:
    def test_default_client_cert_and_key_use_tls_dir(self, monkeypatch, tmp_path):
        import cb_http

        monkeypatch.setattr(cb_http, "_TLS_DIR", tmp_path)
        assert cb_http._default_client_cert() == tmp_path / "middleware-client.pem"
        assert cb_http._default_client_key() == tmp_path / "middleware-client.key"

    def test_default_ca_file_uses_env_override(self, monkeypatch):
        import cb_http

        monkeypatch.setenv("MTLS_CA_FILE", "/tmp/custom-root.pem")
        assert cb_http._default_ca_file() == Path("/tmp/custom-root.pem")

    def test_default_ca_file_uses_mkcert_output(self, monkeypatch):
        import cb_http

        class Result:
            stdout = "/tmp/mkcert-root\n"

        monkeypatch.delenv("MTLS_CA_FILE", raising=False)
        monkeypatch.setattr(cb_http.subprocess, "run", lambda *args, **kwargs: Result())
        assert cb_http._default_ca_file() == Path("/tmp/mkcert-root/rootCA.pem")

    def test_default_ca_file_falls_back_when_mkcert_missing(self, monkeypatch, tmp_path):
        import cb_http

        monkeypatch.delenv("MTLS_CA_FILE", raising=False)
        monkeypatch.setattr(cb_http, "_TLS_DIR", tmp_path)

        def _raise(*args, **kwargs):
            raise OSError("mkcert missing")

        monkeypatch.setattr(cb_http.subprocess, "run", _raise)
        assert cb_http._default_ca_file() == tmp_path / "rootCA.pem"

    def test_default_ca_file_falls_back_on_called_process_error(self, monkeypatch, tmp_path):
        import cb_http

        monkeypatch.delenv("MTLS_CA_FILE", raising=False)
        monkeypatch.setattr(cb_http, "_TLS_DIR", tmp_path)

        def _raise(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "mkcert")

        monkeypatch.setattr(cb_http.subprocess, "run", _raise)
        assert cb_http._default_ca_file() == tmp_path / "rootCA.pem"

    def test_client_and_key_path_use_env_when_present(self, monkeypatch):
        import cb_http

        monkeypatch.setenv("MTLS_CLIENT_CERT", "/tmp/cert.pem")
        monkeypatch.setenv("MTLS_CLIENT_KEY", "/tmp/key.pem")
        assert cb_http.client_cert_path() == Path("/tmp/cert.pem")
        assert cb_http.client_key_path() == Path("/tmp/key.pem")

    def test_client_and_key_path_use_defaults_when_env_missing(self, monkeypatch, tmp_path):
        import cb_http

        monkeypatch.delenv("MTLS_CLIENT_CERT", raising=False)
        monkeypatch.delenv("MTLS_CLIENT_KEY", raising=False)
        monkeypatch.setattr(cb_http, "_default_client_cert", lambda: tmp_path / "d-cert.pem")
        monkeypatch.setattr(cb_http, "_default_client_key", lambda: tmp_path / "d-key.pem")
        assert cb_http.client_cert_path() == tmp_path / "d-cert.pem"
        assert cb_http.client_key_path() == tmp_path / "d-key.pem"

    def test_ca_file_path_prefers_config_value(self, monkeypatch):
        import cb_http
        import config

        monkeypatch.setattr(config, "MTLS_CA_FILE", "/tmp/config-ca.pem", raising=False)
        assert cb_http.ca_file_path() == Path("/tmp/config-ca.pem")

    def test_ca_file_path_uses_default_when_config_empty(self, monkeypatch, tmp_path):
        import cb_http
        import config

        monkeypatch.setattr(config, "MTLS_CA_FILE", "", raising=False)
        monkeypatch.setattr(cb_http, "_default_ca_file", lambda: tmp_path / "fallback-ca.pem")
        assert cb_http.ca_file_path() == tmp_path / "fallback-ca.pem"


class TestMtlsAndRequestKwargs:
    def test_mtls_disabled_for_non_https(self, monkeypatch):
        import cb_http
        import config

        monkeypatch.setattr(config, "CORE_BANKING_URL", "http://core.local", raising=False)
        monkeypatch.delenv("MTLS_DISABLE", raising=False)
        assert cb_http.mtls_enabled() is False

    def test_mtls_disabled_by_env_flag(self, monkeypatch):
        import cb_http
        import config

        monkeypatch.setattr(config, "CORE_BANKING_URL", "https://core.local", raising=False)
        monkeypatch.setenv("MTLS_DISABLE", "true")
        assert cb_http.mtls_enabled() is False

    def test_mtls_enabled_for_https_when_not_disabled(self, monkeypatch):
        import cb_http
        import config

        monkeypatch.setattr(config, "CORE_BANKING_URL", "https://core.local", raising=False)
        monkeypatch.delenv("MTLS_DISABLE", raising=False)
        assert cb_http.mtls_enabled() is True

    def test_request_kwargs_returns_timeout_only_when_mtls_off(self, monkeypatch):
        import cb_http

        monkeypatch.setattr(cb_http, "mtls_enabled", lambda: False)
        assert cb_http.request_kwargs(timeout=5.0) == {"timeout": 5.0}

    def test_request_kwargs_raises_when_cert_or_key_missing(self, monkeypatch, tmp_path):
        import cb_http

        monkeypatch.setattr(cb_http, "mtls_enabled", lambda: True)
        monkeypatch.setattr(cb_http, "client_cert_path", lambda: tmp_path / "missing-cert.pem")
        monkeypatch.setattr(cb_http, "client_key_path", lambda: tmp_path / "missing-key.pem")

        with pytest.raises(RuntimeError, match="mTLS client cert missing"):
            cb_http.request_kwargs()

    def test_request_kwargs_sets_cert_and_verify_true_if_ca_missing(self, monkeypatch, tmp_path):
        import cb_http

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("cert", encoding="utf-8")
        key.write_text("key", encoding="utf-8")

        monkeypatch.setattr(cb_http, "mtls_enabled", lambda: True)
        monkeypatch.setattr(cb_http, "client_cert_path", lambda: cert)
        monkeypatch.setattr(cb_http, "client_key_path", lambda: key)
        monkeypatch.setattr(cb_http, "ca_file_path", lambda: tmp_path / "missing-ca.pem")

        assert cb_http.request_kwargs() == {"cert": (str(cert), str(key)), "verify": True}

    def test_request_kwargs_sets_verify_to_ca_path_when_present(self, monkeypatch, tmp_path):
        import cb_http

        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        ca = tmp_path / "rootCA.pem"
        cert.write_text("cert", encoding="utf-8")
        key.write_text("key", encoding="utf-8")
        ca.write_text("ca", encoding="utf-8")

        monkeypatch.setattr(cb_http, "mtls_enabled", lambda: True)
        monkeypatch.setattr(cb_http, "client_cert_path", lambda: cert)
        monkeypatch.setattr(cb_http, "client_key_path", lambda: key)
        monkeypatch.setattr(cb_http, "ca_file_path", lambda: ca)

        assert cb_http.request_kwargs() == {"cert": (str(cert), str(key)), "verify": str(ca)}


class TestHttpWrappers:
    def test_get_wrapper_merges_kwargs(self, monkeypatch):
        import cb_http

        captured = {}

        monkeypatch.setattr(cb_http, "request_kwargs", lambda timeout=None: {"from_extra": timeout})
        monkeypatch.setattr(
            cb_http.requests,
            "get",
            lambda url, **kwargs: captured.update({"url": url, "kwargs": kwargs}) or "ok",
        )

        out = cb_http.get("https://x", timeout=9, headers={"a": "b"})
        assert out == "ok"
        assert captured == {"url": "https://x", "kwargs": {"from_extra": 9, "headers": {"a": "b"}}}

    def test_post_wrapper_merges_kwargs(self, monkeypatch):
        import cb_http

        captured = {}

        monkeypatch.setattr(cb_http, "request_kwargs", lambda timeout=None: {"from_extra": timeout})
        monkeypatch.setattr(
            cb_http.requests,
            "post",
            lambda url, **kwargs: captured.update({"url": url, "kwargs": kwargs}) or "ok",
        )

        out = cb_http.post("https://x", timeout=4, json={"k": "v"})
        assert out == "ok"
        assert captured == {"url": "https://x", "kwargs": {"from_extra": 4, "json": {"k": "v"}}}

    def test_patch_wrapper_merges_kwargs(self, monkeypatch):
        import cb_http

        captured = {}

        monkeypatch.setattr(cb_http, "request_kwargs", lambda timeout=None: {"from_extra": timeout})
        monkeypatch.setattr(
            cb_http.requests,
            "patch",
            lambda url, **kwargs: captured.update({"url": url, "kwargs": kwargs}) or "ok",
        )

        out = cb_http.patch("https://x", timeout=3, data="p")
        assert out == "ok"
        assert captured == {"url": "https://x", "kwargs": {"from_extra": 3, "data": "p"}}

    def test_delete_wrapper_merges_kwargs(self, monkeypatch):
        import cb_http

        captured = {}

        monkeypatch.setattr(cb_http, "request_kwargs", lambda timeout=None: {"from_extra": timeout})
        monkeypatch.setattr(
            cb_http.requests,
            "delete",
            lambda url, **kwargs: captured.update({"url": url, "kwargs": kwargs}) or "ok",
        )

        out = cb_http.delete("https://x", timeout=2, params={"id": "1"})
        assert out == "ok"
        assert captured == {"url": "https://x", "kwargs": {"from_extra": 2, "params": {"id": "1"}}}
