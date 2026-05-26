"""Unit tests for client_cert.py."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import client_cert
import config
import db
from models import TransactionLog


class TestNormalizeSerial:
    def test_openssl_prefix(self):
        assert client_cert.normalize_serial("serial=1A2B") == "1A2B"

    def test_colons_removed(self):
        assert client_cert.normalize_serial("1A:2B") == "1A2B"


class TestExtractFromHeaders:
    def test_reads_caddy_headers(self):
        info = client_cert.extract_from_headers({
            "X-Client-Cert-Subject": "CN=atm-kiosk",
            "X-Client-Cert-Serial": "serial=ABCD",
        })
        assert info.subject == "CN=atm-kiosk"
        assert info.normalized_serial == "ABCD"

    def test_reads_der_header_when_subject_serial_empty(self):
        from pathlib import Path

        pem = Path.home() / "atm-tls" / "atm-kiosk-client.pem"
        if not pem.is_file():
            pytest.skip("kiosk cert not present")
        import base64
        import subprocess

        der = subprocess.run(
            ["openssl", "x509", "-in", str(pem), "-outform", "DER"],
            capture_output=True,
            check=True,
        ).stdout
        der_b64 = base64.b64encode(der).decode("ascii")
        info = client_cert.extract_from_headers({
            "X-Client-Cert-DER": der_b64,
        })
        assert info.subject
        assert info.normalized_serial


class TestAllowList:
    def test_explicit_serials(self, monkeypatch):
        monkeypatch.setattr(
            config, "CLIENT_CERT_ALLOWED_SERIALS", "aa11, BB22", raising=False
        )
        allowed = client_cert.load_allowed_serials()
        assert allowed == {"AA11", "BB22"}
        assert client_cert.is_serial_allowed("AA11")
        assert not client_cert.is_serial_allowed("FFFF")

    def test_check_and_warn_dedupes(self, monkeypatch):
        monkeypatch.setattr(config, "CLIENT_CERT_ALLOWED_SERIALS", "AA11", raising=False)
        client_cert._alerted_serials.clear()
        info = client_cert.ClientCertInfo(subject="CN=evil", serial="ZZ99")
        msg1 = client_cert.check_and_warn(info, endpoint="/atm/login")
        msg2 = client_cert.check_and_warn(info, endpoint="/atm/login")
        assert msg1 is not None
        assert msg2 is not None
        assert len(client_cert._alerted_serials) == 1

    def test_rejection_detail_when_enforced(self, monkeypatch):
        monkeypatch.setattr(config, "CLIENT_CERT_ALLOWED_SERIALS", "AA11", raising=False)
        monkeypatch.setattr(config, "CLIENT_CERT_ENFORCE_ALLOWLIST", True, raising=False)
        client_cert._alerted_serials.clear()
        info = client_cert.ClientCertInfo(subject="CN=evil", serial="ZZ99")
        assert client_cert.rejection_detail(info, endpoint="/atm/login") is not None

    def test_rejection_detail_missing_serial_allowed(self, monkeypatch):
        monkeypatch.setattr(config, "CLIENT_CERT_ENFORCE_ALLOWLIST", True, raising=False)
        info = client_cert.ClientCertInfo(subject=None, serial=None)
        assert client_cert.rejection_detail(info, endpoint="/atm/login") is None

    def test_rejection_detail_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "CLIENT_CERT_ALLOWED_SERIALS", "AA11", raising=False)
        monkeypatch.setattr(config, "CLIENT_CERT_ENFORCE_ALLOWLIST", False, raising=False)
        info = client_cert.ClientCertInfo(subject="CN=evil", serial="ZZ99")
        assert client_cert.rejection_detail(info, endpoint="/atm/login") is None


@pytest.mark.db
class TestMonitorScan:
    def test_scan_finds_unknown_serial(self, middleware_db, monkeypatch):
        monkeypatch.setattr(config, "CLIENT_CERT_ALLOWED_SERIALS", "ALLOWED1", raising=False)
        client_cert._alerted_serials.clear()
        with db.db_session() as s:
            s.add(
                TransactionLog(
                    log_id="log-1",
                    created_at=datetime.now(timezone.utc),
                    channel="ATM_WEB",
                    http_method="POST",
                    endpoint="/atm/login",
                    response_status_code=200,
                    outcome="success",
                    client_cert_serial="UNKNOWN99",
                )
            )
        client_cert._scan_recent_logs()
        assert "UNKNOWN99" in client_cert._alerted_serials
