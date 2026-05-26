"""Unit tests for kiosk → middleware mTLS client helpers."""

from pathlib import Path
from unittest.mock import patch

import pytest

import mw_http


class TestMtlsEnabled:
    def test_https_local_enabled(self):
        assert mw_http.mtls_enabled("https://mw.local") is True

    def test_http_disabled(self):
        assert mw_http.mtls_enabled("http://127.0.0.1:8000") is False

    def test_disable_env(self, monkeypatch):
        monkeypatch.setenv("MTLS_DISABLE", "1")
        assert mw_http.mtls_enabled("https://mw.local") is False


class TestRequestKwargs:
    def test_no_mtls_uses_tls_verify(self, monkeypatch):
        monkeypatch.setenv("MTLS_DISABLE", "1")
        kw = mw_http.request_kwargs("https://mw.local")
        assert "cert" not in kw
        assert kw["verify"] is True or isinstance(kw["verify"], str)

    def test_mtls_requires_cert_files(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MTLS_DISABLE", raising=False)
        monkeypatch.setenv("ATM_TLS_DIR", str(tmp_path))
        with pytest.raises(RuntimeError, match="gen_kiosk_client_cert"):
            mw_http.request_kwargs("https://mw.local")

    def test_mtls_adds_cert_when_present(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MTLS_DISABLE", raising=False)
        monkeypatch.setenv("ATM_TLS_DIR", str(tmp_path))
        cert = tmp_path / "atm-kiosk-client.pem"
        key = tmp_path / "atm-kiosk-client.key"
        cert.write_text("x")
        key.write_text("y")
        ca = tmp_path / "mkcert-rootCA.pem"
        ca.write_text("z")
        kw = mw_http.request_kwargs("https://mw.local")
        assert kw["cert"] == (str(cert), str(key))
        assert kw["verify"] == str(ca)
