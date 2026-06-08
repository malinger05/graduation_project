"""
Locust load/performance tests for the ATM middleware (Layer 2).

IMPORTANT — log in first, separately:
  Core Banking rate-limits /atm/login to 10/min per IP (anti PIN brute force),
  and all middleware traffic shares one IP. So we DO NOT log in during the load
  run. Instead:
    1) python loadtest/seed_accounts.py --count 50     # create test accounts
    2) python loadtest/warm_sessions.py --count 15      # reusable session tokens
    3) locust -f loadtest/locustfile.py ...             # load test reuses them
  The endpoints under load (deposit/withdraw/balance) are NOT rate-limited.

Scenario switches (env vars; see loadtest/README.md):
  SAME_ACCOUNT=1   all users hit ONE session/account  -> lock contention
  SAME_ACCOUNT=0   users spread across sessions         -> throughput (default)
  ACK_DISABLE=1    skip /atm/ack after withdraw         -> reversal timer
  WRITE_AMOUNT=10  deposit/withdraw amount              -> default 10

Target (see common.py):
  MW_HOST=http://127.0.0.1:8000   isolate app + inline blockchain (default)
  MW_HOST=https://mw.local        realistic, through Caddy mTLS

Example:
  locust -f loadtest/locustfile.py --host $MW_HOST \\
         --headless -u 100 -r 20 -t 5m --csv=loadtest/results
"""

from __future__ import annotations

import itertools
import os
import threading
import uuid

from locust import HttpUser, between, task

import common

SAME_ACCOUNT = os.environ.get("SAME_ACCOUNT", "0").strip() in ("1", "true", "yes")
ACK_DISABLE = os.environ.get("ACK_DISABLE", "0").strip() in ("1", "true", "yes")
WRITE_AMOUNT = float(os.environ.get("WRITE_AMOUNT", "10"))

# Pre-warmed sessions (created by warm_sessions.py) — reused, never re-logged-in.
_SESSIONS = common.load_sessions()
_rr = itertools.cycle(range(len(_SESSIONS)))
_rr_lock = threading.Lock()


def _next_session() -> dict:
    if SAME_ACCOUNT:
        return _SESSIONS[0]
    with _rr_lock:
        return _SESSIONS[next(_rr)]


class AtmUser(HttpUser):
    """Reuses a warmed session token and transacts repeatedly (no login)."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        # Apply mTLS material when the target is an https://*.local host.
        tls = common.mtls_kwargs(self.host or common.MW_HOST)
        if "cert" in tls:
            self.client.cert = tls["cert"]
        self.client.verify = tls.get("verify", True)
        self.token = _next_session()["sessionToken"]

    def _auth(self) -> dict:
        return {"X-Session-Token": self.token}

    def _idem(self) -> dict:
        return {"X-Session-Token": self.token, "Idempotency-Key": str(uuid.uuid4())}

    @task(5)
    def balance(self) -> None:
        self.client.get("/atm/balance", headers=self._auth(), name="GET /atm/balance")

    @task(2)
    def transactions(self) -> None:
        self.client.get("/atm/transactions", headers=self._auth(), name="GET /atm/transactions")

    @task(2)
    def deposit(self) -> None:
        with self.client.post(
            "/atm/deposit",
            json={"amount": WRITE_AMOUNT},
            headers=self._idem(),
            name="POST /atm/deposit",
            catch_response=True,
        ) as resp:
            if resp.ok and resp.json().get("status") == "SUCCESS":
                resp.success()
            else:
                resp.failure(f"deposit failed: HTTP {resp.status_code}")

    @task(1)
    def withdraw(self) -> None:
        with self.client.post(
            "/atm/withdraw",
            json={"amount": WRITE_AMOUNT},
            headers=self._idem(),
            name="POST /atm/withdraw",
            catch_response=True,
        ) as resp:
            if not resp.ok:
                # 400 = insufficient funds (only likely if an account drains).
                resp.failure(f"withdraw failed: HTTP {resp.status_code}")
                return
            resp.success()
            tx_id = resp.json().get("middlewareTxId")

        # Confirm dispense so Core Banking doesn't auto-reverse the debit,
        # unless we're deliberately testing the reversal timer.
        if tx_id is not None and not ACK_DISABLE:
            self.client.post(
                "/atm/ack",
                json={"middlewareTxId": tx_id},
                headers=self._auth(),
                name="POST /atm/ack",
            )
