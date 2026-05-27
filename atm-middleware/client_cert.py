"""
client_cert.py — mTLS client certificate metadata from Caddy + allow-list monitoring.

Caddy forwards TLS client certificate subject and serial via headers on mw.local.
Values are stored on transaction_logs rows and checked against an allow-list built
from known client PEM files under ~/atm-tls (or CLIENT_CERT_ALLOWED_SERIALS).
Unknown serials return HTTP 403 when CLIENT_CERT_ENFORCE_ALLOWLIST is enabled (default).
"""

from __future__ import annotations

import base64
import binascii
import os
import subprocess
import tempfile
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import config
import db
from models import TransactionLog

HEADER_SUBJECT = "X-Client-Cert-Subject"
HEADER_SERIAL = "X-Client-Cert-Serial"
HEADER_CERT_DER = "X-Client-Cert-DER"

_current: ContextVar[ClientCertInfo | None] = ContextVar("client_cert", default=None)
_alerted_serials: set[str] = set()
_alert_lock = threading.Lock()


@dataclass(frozen=True)
class ClientCertInfo:
    subject: str | None
    serial: str | None

    @property
    def normalized_serial(self) -> str | None:
        return normalize_serial(self.serial)


def normalize_serial(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if s.lower().startswith("serial="):
        s = s.split("=", 1)[1].strip()
    s = s.replace(":", "").upper()
    # Convert decimal serial (from Caddy) to hex to match OpenSSL format
    if s.isdigit():
        s = format(int(s), "X")
    return s or None


def _parse_der_b64(der_b64: str) -> ClientCertInfo | None:
    """Parse subject and serial from a base64 DER client certificate (Caddy header)."""
    raw = der_b64.strip()
    if not raw:
        return None
    try:
        der = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        try:
            der = base64.b64decode(raw)
        except (ValueError, binascii.Error):
            return None
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".der", delete=False) as tmp:
            tmp.write(der)
            path = tmp.name
        subj = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-in", path, "-noout", "-subject"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        ser = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-in", path, "-noout", "-serial"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        subject = subj.stdout.strip()
        if subject.lower().startswith("subject="):
            subject = subject.split("=", 1)[1].strip()
        return ClientCertInfo(subject=subject or None, serial=ser.stdout.strip() or None)
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def extract_from_headers(headers: dict[str, str]) -> ClientCertInfo:
    """Read cert metadata forwarded by Caddy (case-insensitive header lookup)."""
    lower = {k.lower(): v for k, v in headers.items()}
    subject = (lower.get(HEADER_SUBJECT.lower()) or "").strip() or None
    serial = (lower.get(HEADER_SERIAL.lower()) or "").strip() or None
    if not subject and not serial:
        der_hdr = (lower.get(HEADER_CERT_DER.lower()) or "").strip()
        if der_hdr:
            parsed = _parse_der_b64(der_hdr)
            if parsed:
                return parsed
    return ClientCertInfo(subject=subject, serial=serial)


def set_current(info: ClientCertInfo | None) -> None:
    _current.set(info)


def current() -> ClientCertInfo | None:
    return _current.get()


def _tls_dir() -> Path:
    raw = os.environ.get("ATM_TLS_DIR", "").strip()
    return Path(raw) if raw else Path.home() / "atm-tls"


def _serial_from_pem(pem_path: Path) -> str | None:
    if not pem_path.is_file():
        return None
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(pem_path), "-noout", "-serial"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return normalize_serial(out.stdout)
    except (subprocess.SubprocessError, OSError):
        return None


def _default_client_pem_paths() -> list[Path]:
    d = _tls_dir()
    return [
        d / "atm-kiosk-client.pem",
        d / "admin-staff-client.pem",
        d / "middleware-client.pem",
    ]


def load_allowed_serials() -> set[str]:
    """
    Allowed client certificate serial numbers.
    CLIENT_CERT_ALLOWED_SERIALS (comma-separated) overrides auto-discovery.
    """
    explicit = config.CLIENT_CERT_ALLOWED_SERIALS.strip()
    if explicit:
        return {
            s
            for part in explicit.split(",")
            if (s := normalize_serial(part.strip()))
        }

    allowed: set[str] = set()
    for path in _default_client_pem_paths():
        serial = _serial_from_pem(path)
        if serial:
            allowed.add(serial)
    return allowed


def is_serial_allowed(serial: str | None) -> bool:
    """True if serial is empty (no mTLS metadata) or on the allow-list."""
    norm = normalize_serial(serial)
    if not norm:
        return True
    return norm in load_allowed_serials()


def rejection_detail(info: ClientCertInfo | None, *, endpoint: str) -> str | None:
    """
    When enforcement is enabled, return an error message if the client cert serial
    was forwarded (Caddy/mw.local) but is not on the allow-list. Missing serial
    is allowed so direct 127.0.0.1:8000 access and unit tests still work.
    """
    if not config.CLIENT_CERT_ENFORCE_ALLOWLIST:
        return None
    norm = info.normalized_serial if info else None
    if not norm or is_serial_allowed(norm):
        return None
    check_and_warn(info, endpoint=endpoint)
    return "Client certificate not on allow-list"


def check_and_warn(info: ClientCertInfo | None, *, endpoint: str) -> str | None:
    """
    If serial is present but not allowed, return a warning message (once per serial).
  """
    norm = info.normalized_serial if info else None
    if not norm or is_serial_allowed(norm):
        return None

    msg = f"client certificate serial not on allow-list: {norm}"
    with _alert_lock:
        if norm not in _alerted_serials:
            _alerted_serials.add(norm)
            subject = info.subject if info else "?"
            print(
                f"[ClientCertMonitor] ALERT unknown client cert serial={norm} "
                f"subject={subject!r} endpoint={endpoint}"
            )
    return msg


def monitor_loop(stop_event: threading.Event) -> None:
    """Background scan of recent audit rows for unknown client cert serials."""
    interval = max(30, config.CLIENT_CERT_MONITOR_INTERVAL_SECONDS)
    while not stop_event.wait(interval):
        if not config.CLIENT_CERT_MONITOR_ENABLED or not db.is_enabled():
            continue
        try:
            _scan_recent_logs()
        except Exception as exc:
            print(f"[ClientCertMonitor] scan failed: {exc}")


def _scan_recent_logs() -> None:
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select

    allowed = load_allowed_serials()
    if not allowed:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=config.CLIENT_CERT_MONITOR_LOOKBACK_SECONDS
    )
    with db.db_session() as s:
        rows = s.execute(
            select(TransactionLog.client_cert_serial, TransactionLog.endpoint)
            .where(TransactionLog.created_at >= cutoff)
            .where(TransactionLog.client_cert_serial.isnot(None))
            .distinct()
        ).all()

    for serial, endpoint in rows:
        norm = normalize_serial(serial)
        if norm and norm not in allowed:
            check_and_warn(
                ClientCertInfo(subject=None, serial=serial),
                endpoint=endpoint or "?",
            )


def format_cert_audit_fields(info: ClientCertInfo | None) -> dict[str, str | None]:
    if not info:
        return {"client_cert_subject": None, "client_cert_serial": None}
    return {
        "client_cert_subject": info.subject,
        "client_cert_serial": info.normalized_serial,
    }
