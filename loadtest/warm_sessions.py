"""
warm_sessions.py — log in a small set of seeded accounts SLOWLY (under Core
Banking's 10/min-per-IP login rate limit) and save their session tokens to
loadtest/sessions.json.

Why: every middleware->Core Banking call shares one source IP, and each ATM
login costs two rate-limited hits (/atm/resolve-card + /atm/login). So we can
only do a few logins per minute. The load test then REUSES these sessions and
loads the non-rate-limited endpoints (deposit/withdraw/balance).

Sessions stay alive as long as they're used (idle TTL is refreshed on every
request), so run this immediately before the Locust run.

Usage:
  python loadtest/warm_sessions.py --count 15
  python loadtest/warm_sessions.py --count 20 --delay 15

Env: MW_HOST (default http://127.0.0.1:8000), plus mTLS vars (see common.py).
"""

from __future__ import annotations

import argparse
import sys
import time

import requests

import common


def _login(sess: requests.Session, account: dict) -> str | None:
    r = sess.post(
        f"{common.MW_HOST}/atm/login",
        json={"cardNumber": account["cardNumber"], "pin": account["pin"]},
        timeout=(3, 15),
        **common.mtls_kwargs(common.MW_HOST),
    )
    # 502 = middleware saw Core Banking 429 (rate limited). Signal caller to wait.
    if r.status_code == 502:
        return "RATE_LIMITED"
    if not r.ok:
        return None
    body = r.json()
    if body.get("status") == "ok" and body.get("sessionToken"):
        return body["sessionToken"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Warm up reusable ATM sessions for load testing.")
    ap.add_argument("--count", type=int, default=15, help="number of sessions to create")
    ap.add_argument("--delay", type=float, default=15.0,
                    help="seconds between logins (keep <= ~4/min to respect rate limit)")
    args = ap.parse_args()

    accounts = common.load_accounts()
    if args.count > len(accounts):
        print(f"Only {len(accounts)} seeded accounts available; using all of them.")
        args.count = len(accounts)

    sess = requests.Session()
    sess.trust_env = False
    warmed: list[dict] = []
    i = 0
    while len(warmed) < args.count and i < len(accounts):
        acct = accounts[i]
        token = _login(sess, acct)
        if token == "RATE_LIMITED":
            print("  rate limited (429/502) — waiting 60s for the bucket to refill...")
            time.sleep(60)
            continue  # retry same account
        if token:
            warmed.append({"accountNumber": acct["accountNumber"], "sessionToken": token})
            print(f"  [{len(warmed)}/{args.count}] session for {acct['accountNumber']}")
        else:
            print(f"  login failed for {acct['accountNumber']} — skipping", file=sys.stderr)
        i += 1
        if len(warmed) < args.count:
            time.sleep(args.delay)

    if not warmed:
        raise SystemExit("No sessions warmed. Is the stack up and are accounts seeded?")

    common.save_sessions(warmed)
    print(f"\nWrote {len(warmed)} session(s) to {common.SESSIONS_FILE}")
    print("Run the load test now (sessions stay alive while in use).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
