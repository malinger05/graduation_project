"""
HTTP client for Core Banking's /admin/transactions/* endpoints.

Authenticates with the X-Service-Token header. The shared secret matches
`app.middleware.service-token` on the Spring Boot side.
"""

from __future__ import annotations

from typing import Any

import requests

import config


class AdminClient:
    def __init__(self,
                 core_banking_url: str | None = None,
                 service_token: str | None = None):
        self.base_url = (core_banking_url or config.CORE_BANKING_URL).rstrip("/")
        self.service_token = (service_token or config.SERVICE_TOKEN).strip()

    def _headers(self) -> dict:
        if not self.service_token:
            raise RuntimeError(
                "MIDDLEWARE_SERVICE_TOKEN is not set. "
                "The blockchain worker cannot authenticate with Core Banking."
            )
        return {
            "X-Service-Token": self.service_token,
            "Content-Type":    "application/json",
        }

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get_pending_submit(self, limit: int = 25, max_attempts: int = 8) -> list[dict]:
        resp = requests.get(
            f"{self.base_url}/admin/transactions/pending-submit",
            headers=self._headers(),
            params={"limit": limit, "maxAttempts": max_attempts},
            timeout=(3, 12),
        )
        resp.raise_for_status()
        return resp.json()

    def get_submitted(self, limit: int = 25) -> list[dict]:
        resp = requests.get(
            f"{self.base_url}/admin/transactions/submitted",
            headers=self._headers(),
            params={"limit": limit},
            timeout=(3, 12),
        )
        resp.raise_for_status()
        return resp.json()

    def get_for_tamper_check(self, since_iso: str | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if since_iso:
            params["since"] = since_iso
        resp = requests.get(
            f"{self.base_url}/admin/transactions/for-tamper-check",
            headers=self._headers(),
            params=params,
            timeout=(3, 12),
        )
        resp.raise_for_status()
        return resp.json()

    # ── Writes ────────────────────────────────────────────────────────────────

    def patch_blockchain(self,
                         transaction_id: int,
                         canonical_hash: str | None = None,
                         blockchain_tx: str | None = None,
                         submit_error: str | None = None) -> dict:
        resp = requests.patch(
            f"{self.base_url}/admin/transactions/{transaction_id}/blockchain",
            headers=self._headers(),
            json={
                "canonicalHash": canonical_hash,
                "blockchainTx":  blockchain_tx,
                "submitError":   submit_error,
            },
            timeout=(3, 12),
        )
        resp.raise_for_status()
        return resp.json()

    def patch_confirm(self, transaction_id: int) -> dict:
        resp = requests.patch(
            f"{self.base_url}/admin/transactions/{transaction_id}/confirm",
            headers=self._headers(),
            timeout=(3, 12),
        )
        resp.raise_for_status()
        return resp.json()

    def patch_tampered(self, transaction_id: int, reason: str) -> dict:
        resp = requests.patch(
            f"{self.base_url}/admin/transactions/{transaction_id}/tampered",
            headers=self._headers(),
            json={"reason": reason},
            timeout=(3, 12),
        )
        resp.raise_for_status()
        return resp.json()
