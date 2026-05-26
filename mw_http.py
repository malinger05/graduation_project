"""
HTTP client helpers for Layer 1 → middleware (https://mw.local behind Caddy mTLS).

Requires ./scripts/gen_kiosk_client_cert.sh (creates ~/atm-tls/atm-kiosk-client.pem).
Set MTLS_DISABLE=1 to skip client certs (e.g. unit tests with mocked HTTP).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from cert_header_util import cert_audit_headers_from_pem

_DEFAULT_TLS_DIR = Path.home() / "atm-tls"


def _tls_dir() -> Path:
    raw = os.environ.get("ATM_TLS_DIR", "").strip()
    return Path(raw) if raw else _DEFAULT_TLS_DIR


def _default_client_cert() -> Path:
    return _tls_dir() / "atm-kiosk-client.pem"


def _default_client_key() -> Path:
    return _tls_dir() / "atm-kiosk-client.key"


def _default_ca_file() -> Path:
    env_ca = os.environ.get("MTLS_CA_FILE", "").strip()
    if env_ca:
        return Path(env_ca)
    copied = _tls_dir() / "mkcert-rootCA.pem"
    if copied.is_file():
        return copied
    try:
        caroot = subprocess.run(
            ["mkcert", "-CAROOT"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(caroot) / "rootCA.pem"
    except (OSError, subprocess.CalledProcessError):
        return _tls_dir() / "rootCA.pem"


def client_cert_path() -> Path:
    raw = os.environ.get("MTLS_KIOSK_CERT", "").strip()
    return Path(raw) if raw else _default_client_cert()


def client_key_path() -> Path:
    raw = os.environ.get("MTLS_KIOSK_KEY", "").strip()
    return Path(raw) if raw else _default_client_key()


def ca_file_path() -> Path:
    return _default_ca_file()


def mtls_enabled(base_url: str | None = None) -> bool:
    if os.environ.get("MTLS_DISABLE", "").lower() in ("1", "true", "yes"):
        return False
    url = (base_url or os.environ.get("MIDDLEWARE_URL", "https://mw.local")).strip()
    if not url.lower().startswith("https://"):
        return False
    host = urlparse(url).hostname or ""
    return host.endswith(".local") or host in ("localhost", "127.0.0.1")


def request_kwargs(
    base_url: str | None = None,
    timeout: tuple[float, float] | float | None = None,
) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if timeout is not None:
        kw["timeout"] = timeout
    if not mtls_enabled(base_url):
        from tls_verify import requests_verify

        verify_url = base_url or os.environ.get("MIDDLEWARE_URL", "https://mw.local")
        kw["verify"] = requests_verify(verify_url)
        return kw

    cert = client_cert_path()
    key = client_key_path()
    if not cert.is_file() or not key.is_file():
        raise RuntimeError(
            "Kiosk mTLS client cert missing. Run: ./scripts/gen_kiosk_client_cert.sh\n"
            f"  Expected: {cert} and {key}"
        )
    kw["cert"] = (str(cert), str(key))
    ca = ca_file_path()
    kw["verify"] = str(ca) if ca.is_file() else True
    kw["headers"] = cert_audit_headers_from_pem(cert)
    return kw


def _with_headers(url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    extra = request_kwargs(url, kwargs.pop("timeout", None))
    merged = {**extra, **kwargs}
    headers = dict(merged.get("headers") or {})
    headers.update(extra.get("headers") or {})
    if headers:
        merged["headers"] = headers
    return merged


def get(url: str, **kwargs: Any) -> requests.Response:
    return requests.get(url, **_with_headers(url, kwargs))


def post(url: str, **kwargs: Any) -> requests.Response:
    return requests.post(url, **_with_headers(url, kwargs))


def patch(url: str, **kwargs: Any) -> requests.Response:
    return requests.patch(url, **_with_headers(url, kwargs))
