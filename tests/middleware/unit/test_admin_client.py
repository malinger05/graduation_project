"""Unit tests for admin_client.py — mocked HTTP to Core Banking."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from admin_client import AdminClient


@pytest.fixture
def client():
    return AdminClient(core_banking_url="http://cb.test", service_token="secret-token")


class TestAdminClientInit:
    def test_strips_trailing_slash(self):
        c = AdminClient("http://cb.test/", "tok")
        assert c.base_url == "http://cb.test"

    @patch("admin_client.config.SERVICE_TOKEN", "")
    def test_headers_require_token(self):
        c = AdminClient("http://cb.test", "")
        with pytest.raises(RuntimeError, match="SERVICE_TOKEN"):
            c._headers()


class TestAdminClientReads:
    @patch("admin_client.requests.get")
    def test_get_pending_submit(self, mock_get, client):
        mock_get.return_value = MagicMock(
            ok=True, status_code=200, json=lambda: [{"transactionId": 1}]
        )
        rows = client.get_pending_submit(limit=10, max_attempts=5)
        assert rows[0]["transactionId"] == 1
        mock_get.assert_called_once()
        assert mock_get.call_args.kwargs["params"] == {"limit": 10, "maxAttempts": 5}
        assert mock_get.call_args.kwargs["headers"]["X-Service-Token"] == "secret-token"

    @patch("admin_client.requests.get")
    def test_get_submitted(self, mock_get, client):
        mock_get.return_value = MagicMock(ok=True, json=lambda: [])
        client.get_submitted(limit=5)
        assert "submitted" in mock_get.call_args.args[0]

    @patch("admin_client.requests.get")
    def test_get_for_tamper_check_with_since(self, mock_get, client):
        mock_get.return_value = MagicMock(ok=True, json=lambda: [])
        client.get_for_tamper_check(since_iso="2026-01-01T00:00:00", limit=50)
        assert mock_get.call_args.kwargs["params"]["since"] == "2026-01-01T00:00:00"

    @patch("admin_client.requests.get")
    def test_get_raises_on_http_error(self, mock_get, client):
        mock_get.return_value = MagicMock(ok=False, raise_for_status=lambda: (_ for _ in ()).throw(
            Exception("404")
        ))
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404")
        mock_get.return_value = mock_resp
        with pytest.raises(Exception):
            client.get_submitted()


class TestAdminClientWrites:
    @patch("admin_client.requests.patch")
    def test_patch_blockchain_payload(self, mock_patch, client):
        mock_patch.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
        client.patch_blockchain(99, canonical_hash="abc", blockchain_tx="0x1", submit_error=None)
        body = mock_patch.call_args.kwargs["json"]
        assert body["canonicalHash"] == "abc"
        assert body["blockchainTx"] == "0x1"
        assert "99" in mock_patch.call_args.args[0]

    @patch("admin_client.requests.patch")
    def test_patch_confirm(self, mock_patch, client):
        mock_patch.return_value = MagicMock(ok=True, json=lambda: {})
        client.patch_confirm(42)
        assert mock_patch.call_args.args[0].endswith("/42/confirm")

    @patch("admin_client.requests.patch")
    def test_patch_tampered(self, mock_patch, client):
        mock_patch.return_value = MagicMock(ok=True, json=lambda: {})
        client.patch_tampered(7, "hash mismatch")
        assert mock_patch.call_args.kwargs["json"]["reason"] == "hash mismatch"
