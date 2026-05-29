"""Unit tests for mTLS certificate expiry helpers."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mtls_cert import cert_not_after, days_until_expiry, needs_renewal


def _write_self_signed(tmp_path: Path, *, days_valid: int) -> Path:
    key = tmp_path / "test.key"
    cert = tmp_path / "test.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            str(days_valid),
            "-nodes",
            "-subj",
            "/CN=test-rotate",
        ],
        check=True,
        capture_output=True,
    )
    return cert


def test_cert_not_after_reads_expiry(tmp_path: Path) -> None:
    cert = _write_self_signed(tmp_path, days_valid=30)
    not_after = cert_not_after(cert)
    assert not_after is not None
    assert not_after > datetime.now(timezone.utc)


def test_days_until_expiry_positive(tmp_path: Path) -> None:
    cert = _write_self_signed(tmp_path, days_valid=60)
    days = days_until_expiry(cert)
    assert days is not None
    assert 55 <= days <= 60


def test_needs_renewal_when_missing(tmp_path: Path) -> None:
    assert needs_renewal(tmp_path / "missing.pem", 30) is True


def test_needs_renewal_within_window(tmp_path: Path) -> None:
    cert = _write_self_signed(tmp_path, days_valid=10)
    assert needs_renewal(cert, renew_before_days=30) is True
    assert needs_renewal(cert, renew_before_days=5) is False


def test_needs_renewal_expired(tmp_path: Path) -> None:
    cert = _write_self_signed(tmp_path, days_valid=1)
    not_after = cert_not_after(cert)
    assert not_after is not None
    past = not_after + timedelta(days=1)
    assert days_until_expiry(cert, now=past) is not None
    assert days_until_expiry(cert, now=past) < 0
    assert needs_renewal(cert, renew_before_days=0, now=past) is True


def test_cert_not_after_missing_file(tmp_path: Path) -> None:
    assert cert_not_after(tmp_path / "missing.pem") is None


def test_cert_not_after_invalid_pem(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate")
    assert cert_not_after(bad) is None


def test_days_until_expiry_missing_file(tmp_path: Path) -> None:
    assert days_until_expiry(tmp_path / "nope.pem") is None
