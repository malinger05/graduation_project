"""Build X-Client-Cert-* audit headers from a local client PEM (same file used for mTLS)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def cert_audit_headers_from_pem(pem_path: Path) -> dict[str, str]:
    """Return subject and serial headers for middleware transaction_logs."""
    if not pem_path.is_file():
        return {}
    try:
        subj = subprocess.run(
            ["openssl", "x509", "-in", str(pem_path), "-noout", "-subject"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        ser = subprocess.run(
            ["openssl", "x509", "-in", str(pem_path), "-noout", "-serial"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError):
        return {}
    subject = subj.stdout.strip()
    if subject.lower().startswith("subject="):
        subject = subject.split("=", 1)[1].strip()
    serial = ser.stdout.strip()
    if not subject and not serial:
        return {}
    headers: dict[str, str] = {}
    if subject:
        headers["X-Client-Cert-Subject"] = subject
    if serial:
        headers["X-Client-Cert-Serial"] = serial
    return headers
