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

    def test_custom_cert_paths(self, monkeypatch, tmp_path):
        cert = tmp_path / "custom.pem"
        key = tmp_path / "custom.key"
        cert.write_text("c")
        key.write_text("k")
        monkeypatch.setenv("MTLS_KIOSK_CERT", str(cert))
        monkeypatch.setenv("MTLS_KIOSK_KEY", str(key))
        monkeypatch.setenv("MTLS_DISABLE", "1")
        assert mw_http.client_cert_path() == cert
        assert mw_http.client_key_path() == key

    def test_ca_file_from_env(self, monkeypatch, tmp_path):
        ca = tmp_path / "ca.pem"
        ca.write_text("ca")
        monkeypatch.setenv("MTLS_CA_FILE", str(ca))
        assert mw_http.ca_file_path() == ca


class TestMwHttpVerbs:
    def test_get_post_patch_delegate(self, monkeypatch):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs
            return "resp"

        monkeypatch.setenv("MTLS_DISABLE", "1")
        monkeypatch.setattr(mw_http.requests, "get", lambda url, **kw: fake_request("GET", url, **kw))
        monkeypatch.setattr(mw_http.requests, "post", lambda url, **kw: fake_request("POST", url, **kw))
        monkeypatch.setattr(mw_http.requests, "patch", lambda url, **kw: fake_request("PATCH", url, **kw))

        assert mw_http.get("https://mw.local/x") == "resp"
        assert captured["method"] == "GET"
        assert mw_http.post("https://mw.local/y") == "resp"
        assert mw_http.patch("https://mw.local/z") == "resp"
