"""Unit tests for client_cert.py."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

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

    def test_decimal_serial_converted_to_hex(self):
        assert client_cert.normalize_serial("255") == "FF"

    def test_empty_returns_none(self):
        assert client_cert.normalize_serial(None) is None
        assert client_cert.normalize_serial("  ") is None


class TestSerialAliases:
    def test_hex_and_decimal_aliases(self):
        aliases = client_cert._serial_aliases("serial=FF")
        assert "FF" in aliases
        assert "255" in aliases

    def test_decimal_input_adds_hex_alias(self):
        aliases = client_cert._serial_aliases("255")
        assert "FF" in aliases


class TestContextAndFormat:
    def test_set_and_current(self):
        info = client_cert.ClientCertInfo(subject="CN=x", serial="AA")
        client_cert.set_current(info)
        assert client_cert.current() == info
        client_cert.set_current(None)
        assert client_cert.current() is None

    def test_format_cert_audit_fields(self):
        assert client_cert.format_cert_audit_fields(None) == {
            "client_cert_subject": None,
            "client_cert_serial": None,
        }
        info = client_cert.ClientCertInfo(subject="CN=kiosk", serial="serial=AB")
        fields = client_cert.format_cert_audit_fields(info)
        assert fields["client_cert_subject"] == "CN=kiosk"
        assert fields["client_cert_serial"] == "AB"


class TestParseDer:
    def test_invalid_der_returns_none(self):
        assert client_cert._parse_der_b64("not-valid-base64!!!") is None
        assert client_cert._parse_der_b64("") is None


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

    def test_scan_skips_when_allowlist_empty(self, middleware_db, monkeypatch):
        client_cert._alerted_serials.clear()
        with patch.object(client_cert, "load_allowed_serials", return_value=set()):
            client_cert._scan_recent_logs()


class TestMonitorLoop:
    def test_monitor_loop_disabled_exits_quickly(self, monkeypatch):
        import threading

        monkeypatch.setattr(config, "CLIENT_CERT_MONITOR_ENABLED", False, raising=False)
        stop = threading.Event()
        stop.set()
        client_cert.monitor_loop(stop)

    def test_load_allowed_from_pem_paths(self, monkeypatch, tmp_path):
        pem = tmp_path / "atm-kiosk-client.pem"
        pem.write_text("dummy")
        monkeypatch.setattr(config, "CLIENT_CERT_ALLOWED_SERIALS", "", raising=False)
        with patch.object(client_cert, "_default_client_pem_paths", return_value=[pem]):
            with patch.object(client_cert, "_serial_from_pem", return_value="CAFE01"):
                allowed = client_cert.load_allowed_serials()
                assert allowed == {"CAFE01"}
                assert client_cert.is_serial_allowed("CAFE01")
