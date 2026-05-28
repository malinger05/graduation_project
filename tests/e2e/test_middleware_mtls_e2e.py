"""Full-stack e2e tests for middleware mTLS and secure routing.

These tests are opt-in and run only when E2E_ENABLED=1.
They expect the local stack to be running (Caddy + middleware + core banking).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import requests


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


E2E_ENABLED = _env_bool("E2E_ENABLED", "0")

if not E2E_ENABLED:
    pytestmark = pytest.mark.skip(reason="Set E2E_ENABLED=1 to run full-stack e2e tests.")


def _tls_dir() -> Path:
    return Path(os.environ.get("ATM_TLS_DIR", str(Path.home() / "atm-tls")))


def _mw_url() -> str:
    return os.environ.get("E2E_MW_URL", "https://mw.local").rstrip("/")


def _root_ca() -> str:
    return os.environ.get("E2E_ROOT_CA", str(_tls_dir() / "mkcert-rootCA.pem"))


def _kiosk_cert_pair() -> tuple[str, str]:
    cert = os.environ.get("E2E_KIOSK_CERT", str(_tls_dir() / "atm-kiosk-client.pem"))
    key = os.environ.get("E2E_KIOSK_KEY", str(_tls_dir() / "atm-kiosk-client.key"))
    return cert, key


def _admin_cert_pair() -> tuple[str, str]:
    cert = os.environ.get("E2E_ADMIN_CERT", str(_tls_dir() / "admin-staff-client.pem"))
    key = os.environ.get("E2E_ADMIN_KEY", str(_tls_dir() / "admin-staff-client.key"))
    return cert, key


def _service_token() -> str:
    return os.environ.get("E2E_SERVICE_TOKEN", "").strip()


def _must_exist(path: str) -> None:
    if not Path(path).is_file():
        pytest.skip(f"Missing required file: {path}")


def _kiosk_session_token() -> str:
    card = os.environ.get("E2E_TEST_CARD", "").strip()
    pin = os.environ.get("E2E_TEST_PIN", "").strip()
    if not card or not pin:
        pytest.skip("Set E2E_TEST_CARD and E2E_TEST_PIN for authenticated e2e flows.")

    cert, key = _kiosk_cert_pair()
    ca = _root_ca()
    _must_exist(cert)
    _must_exist(key)
    _must_exist(ca)

    resp = requests.post(
        f"{_mw_url()}/atm/login",
        json={"cardNumber": card, "pin": pin},
        cert=(cert, key),
        verify=ca,
        timeout=15,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "ok"
    token = body.get("sessionToken")
    assert token
    return token


@pytest.mark.integration
def test_e2e_mw_local_requires_client_cert():
    """Request without client cert should fail at TLS/proxy boundary."""
    ca = _root_ca()
    _must_exist(ca)

    try:
        resp = requests.get(f"{_mw_url()}/health", verify=ca, timeout=10)
    except requests.exceptions.SSLError:
        return
    except requests.exceptions.ConnectionError as exc:
        pytest.skip(f"Stack not reachable: {exc}")

    # Depending on proxy behavior/version, it may reject with 4xx if handshake reached HTTP layer.
    assert resp.status_code in (400, 401, 403, 495, 496)


@pytest.mark.integration
def test_e2e_kiosk_cert_allows_atm_login():
    """Known kiosk cert should pass mTLS and allow ATM login flow."""
    token = _kiosk_session_token()
    assert isinstance(token, str) and token


@pytest.mark.integration
def test_e2e_admin_unlock_requires_service_token_with_admin_cert():
    """Admin mTLS identity alone is not enough; service token is also required."""
    cert, key = _admin_cert_pair()
    ca = _root_ca()
    _must_exist(cert)
    _must_exist(key)
    _must_exist(ca)

    url = f"{_mw_url()}/atm/admin/login-unlock"
    payload = {"accountNumber": os.environ.get("E2E_TEST_ACCOUNT", "ACC-PLACEHOLDER")}

    no_token = requests.post(url, json=payload, cert=(cert, key), verify=ca, timeout=15)
    assert no_token.status_code == 401

    token = _service_token()
    if not token:
        pytest.skip("Set E2E_SERVICE_TOKEN to verify positive admin unlock path.")
    with_token = requests.post(
        url,
        json=payload,
        headers={"X-Service-Token": token},
        cert=(cert, key),
        verify=ca,
        timeout=15,
    )
    assert with_token.status_code == 200
    assert with_token.json().get("status") == "ok"


@pytest.mark.integration
def test_e2e_withdraw_idempotency_replay_returns_cached_result():
    """Same idempotency key should not create a second withdraw operation."""
    token = _kiosk_session_token()
    cert, key = _kiosk_cert_pair()
    ca = _root_ca()

    headers = {"X-Session-Token": token, "Idempotency-Key": "e2e-withdraw-replay-001"}
    payload = {"amount": float(os.environ.get("E2E_WITHDRAW_AMOUNT", "10"))}

    first = requests.post(
        f"{_mw_url()}/atm/withdraw",
        json=payload,
        headers=headers,
        cert=(cert, key),
        verify=ca,
        timeout=20,
    )
    assert first.status_code == 200

    second = requests.post(
        f"{_mw_url()}/atm/withdraw",
        json=payload,
        headers=headers,
        cert=(cert, key),
        verify=ca,
        timeout=20,
    )
    assert second.status_code == 200
    assert first.json() == second.json()


@pytest.mark.integration
def test_e2e_health_cert_headers_exposes_client_cert_metadata():
    """With valid mTLS cert, middleware reports forwarded cert metadata."""
    cert, key = _kiosk_cert_pair()
    ca = _root_ca()
    _must_exist(cert)
    _must_exist(key)
    _must_exist(ca)

    resp = requests.get(
        f"{_mw_url()}/health/cert-headers",
        cert=(cert, key),
        verify=ca,
        timeout=15,
    )
    assert resp.status_code == 200
    body = resp.json()
    # Caddy should forward at least one cert-related piece of metadata.
    assert body.get("client_cert_subject") or body.get("client_cert_serial")
