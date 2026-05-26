"""mTLS client certificate expiry helpers (used by rotation agent)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def cert_not_after(cert_path: Path) -> datetime | None:
    """Return certificate expiry in UTC, or None if unreadable."""
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-enddate"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if not out.startswith("notAfter="):
        return None
    raw = out.removeprefix("notAfter=").strip()
    try:
        dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc)


def days_until_expiry(cert_path: Path, *, now: datetime | None = None) -> float | None:
    """Days until expiry (negative if already expired). None if cert missing/invalid."""
    if not cert_path.is_file():
        return None
    not_after = cert_not_after(cert_path)
    if not_after is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return (not_after - ref).total_seconds() / 86400.0


def needs_renewal(
    cert_path: Path,
    renew_before_days: float = 30.0,
    *,
    now: datetime | None = None,
) -> bool:
    """True when cert is missing, unreadable, or expires within renew_before_days."""
    days = days_until_expiry(cert_path, now=now)
    if days is None:
        return True
    return days <= renew_before_days
