"""Trust mkcert-issued HTTPS certs for local *.local hostnames (used by requests)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _mkcert_ca() -> Path | None:
    env_ca = os.environ.get("TLS_CA_FILE", "").strip()
    if env_ca:
        p = Path(env_ca)
        return p if p.is_file() else None
    copied = Path.home() / "atm-tls" / "mkcert-rootCA.pem"
    if copied.is_file():
        return copied
    try:
        caroot = subprocess.run(
            ["mkcert", "-CAROOT"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        p = Path(caroot) / "rootCA.pem"
        return p if p.is_file() else None
    except (OSError, subprocess.CalledProcessError):
        return None


def requests_verify(base_url: str) -> bool | str:
    """Pass as verify= to requests.* for https://*.local dev URLs."""
    if not base_url.lower().startswith("https://"):
        return True
    ca = _mkcert_ca()
    return str(ca) if ca else True
