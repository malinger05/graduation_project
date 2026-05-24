"""
HTTP client helpers for Core Banking.

When CORE_BANKING_URL uses https (e.g. https://api.local behind Caddy mTLS),
requests include the middleware client certificate from ~/atm-tls/.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import requests

import config

_DEFAULT_TLS_DIR = Path.home() / "atm-tls"
_TLS_DIR = Path(os.environ.get("ATM_TLS_DIR", str(_DEFAULT_TLS_DIR)))


def _default_client_cert() -> Path:
    return _TLS_DIR / "middleware-client.pem"


def _default_client_key() -> Path:
    return _TLS_DIR / "middleware-client.key"


def _default_ca_file() -> Path:
    env_ca = os.environ.get("MTLS_CA_FILE", "").strip()
    if env_ca:
        return Path(env_ca)
    try:
        caroot = subprocess.run(
            ["mkcert", "-CAROOT"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(caroot) / "rootCA.pem"
    except (OSError, subprocess.CalledProcessError):
        return _TLS_DIR / "rootCA.pem"


def client_cert_path() -> Path:
    raw = os.environ.get("MTLS_CLIENT_CERT", "").strip()
    return Path(raw) if raw else _default_client_cert()


def client_key_path() -> Path:
    raw = os.environ.get("MTLS_CLIENT_KEY", "").strip()
    return Path(raw) if raw else _default_client_key()


def ca_file_path() -> Path:
    return Path(config.MTLS_CA_FILE) if config.MTLS_CA_FILE else _default_ca_file()


def mtls_enabled() -> bool:
    if not config.CORE_BANKING_URL.lower().startswith("https://"):
        return False
    if os.environ.get("MTLS_DISABLE", "").lower() in ("1", "true", "yes"):
        return False
    return True


def request_kwargs(timeout: tuple[float, float] | float | None = None) -> dict[str, Any]:
    """Extra kwargs for requests.* when talking to Core Banking over mTLS."""
    kw: dict[str, Any] = {}
    if timeout is not None:
        kw["timeout"] = timeout
    if not mtls_enabled():
        return kw

    cert = client_cert_path()
    key = client_key_path()
    if not cert.is_file() or not key.is_file():
        raise RuntimeError(
            f"mTLS client cert missing. Run: ./scripts/gen_mtls_client_cert.sh\n"
            f"  Expected: {cert} and {key}"
        )
    kw["cert"] = (str(cert), str(key))

    ca = ca_file_path()
    kw["verify"] = str(ca) if ca.is_file() else True
    return kw


def get(url: str, **kwargs: Any) -> requests.Response:
    extra = request_kwargs(kwargs.pop("timeout", None))
    return requests.get(url, **{**extra, **kwargs})


def post(url: str, **kwargs: Any) -> requests.Response:
    extra = request_kwargs(kwargs.pop("timeout", None))
    return requests.post(url, **{**extra, **kwargs})


def patch(url: str, **kwargs: Any) -> requests.Response:
    extra = request_kwargs(kwargs.pop("timeout", None))
    return requests.patch(url, **{**extra, **kwargs})


def delete(url: str, **kwargs: Any) -> requests.Response:
    extra = request_kwargs(kwargs.pop("timeout", None))
    return requests.delete(url, **{**extra, **kwargs})
