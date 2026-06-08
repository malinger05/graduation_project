"""
seed_accounts.py — create N test accounts (customer + account + card + PIN) in
Core Banking, then write their card numbers + PINs to loadtest/accounts.json so
the Locust scenarios can log in.

Every account is given a large initial balance so withdrawals during a load run
do not deplete it. PIN is the same for all seeded cards (these are throwaway
test accounts only).

Talks to Core Banking with a ROLE_ADMIN JWT, reusing the same credentials the
admin panel uses (POST /auth/login). Defaults hit loopback http://127.0.0.1:8080
directly (no mTLS); set CB_HOST=https://api.local to go through Caddy.

Usage:
  python loadtest/seed_accounts.py --count 50
  python loadtest/seed_accounts.py --count 50 --balance 5000000 --pin 4321

Env:
  CB_HOST            core banking base URL          (default http://127.0.0.1:8080)
  CB_ADMIN_USERNAME  Core Banking admin username    (default admin)
  CB_ADMIN_PASSWORD  Core Banking admin password    (default admin123)
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date

import requests

import common


def _admin_jwt(sess: requests.Session) -> str:
    user = os.environ.get("CB_ADMIN_USERNAME", "admin")
    pw = os.environ.get("CB_ADMIN_PASSWORD", "admin123")
    r = sess.post(
        f"{common.CB_HOST}/auth/login",
        json={"username": user, "password": pw},
        timeout=(3, 10),
        **common.mtls_kwargs(common.CB_HOST),
    )
    if not r.ok:
        raise SystemExit(
            f"Core Banking admin login failed ({r.status_code}). "
            f"Ensure a ROLE_ADMIN user '{user}' exists (POST /auth/register), "
            f"or set CB_ADMIN_USERNAME/CB_ADMIN_PASSWORD.\n{r.text[:200]}"
        )
    return r.json()["token"]


def _seed_one(sess: requests.Session, jwt: str, balance: float, pin: str) -> dict:
    h = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    tls = common.mtls_kwargs(common.CB_HOST)
    tag = uuid.uuid4().hex[:8]

    # 1) Customer
    cust = sess.post(
        f"{common.CB_HOST}/customers",
        headers=h,
        json={
            "firstName": "Load",
            "lastName": f"Test{tag}",
            "nationalId": str(uuid.uuid4().int)[:11],
            "email": f"load+{tag}@example.com",
            "phoneNumber": f"+99890{str(uuid.uuid4().int)[:7]}",
            "dateOfBirth": date(1990, 1, 1).isoformat(),
        },
        timeout=(3, 15),
        **tls,
    )
    cust.raise_for_status()
    customer_id = cust.json()["customerId"]

    # 2) Account
    acc = sess.post(
        f"{common.CB_HOST}/customers/{customer_id}/accounts",
        headers=h,
        json={"initialBalance": balance},
        timeout=(3, 15),
        **tls,
    )
    acc.raise_for_status()
    acc_body = acc.json()
    account_id = acc_body["accountId"]
    account_number = acc_body["accountNumber"]

    # 3) Card (issue returns the full card number)
    card = sess.post(
        f"{common.CB_HOST}/accounts/{account_id}/cards",
        headers=h,
        json={"holderName": "Load Test"},
        timeout=(3, 15),
        **tls,
    )
    card.raise_for_status()
    card_body = card.json()
    card_id = card_body["cardId"]
    card_number = card_body["cardNumber"]

    # 4) PIN (admin set-pin — ROLE_ADMIN)
    setpin = sess.post(
        f"{common.CB_HOST}/atm/set-pin",
        headers=h,
        json={"cardId": str(card_id), "pin": pin},
        timeout=(3, 15),
        **tls,
    )
    setpin.raise_for_status()

    return {
        "cardNumber": card_number,
        "pin": pin,
        "accountId": account_id,
        "accountNumber": account_number,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed test accounts for load testing.")
    ap.add_argument("--count", type=int, default=50, help="number of accounts (default 50)")
    ap.add_argument("--balance", type=float, default=1_000_000.0, help="initial balance each")
    ap.add_argument("--pin", default="1234", help="4-digit PIN for all cards")
    ap.add_argument("--append", action="store_true", help="append to existing accounts.json")
    args = ap.parse_args()

    if not (args.pin.isdigit() and len(args.pin) == 4):
        raise SystemExit("--pin must be exactly 4 digits")

    sess = requests.Session()
    sess.trust_env = False  # bypass local proxy settings for *.local / loopback
    jwt = _admin_jwt(sess)

    existing = common.load_accounts() if (args.append and common.ACCOUNTS_FILE.is_file()) else []
    accounts = list(existing)
    for i in range(args.count):
        try:
            acct = _seed_one(sess, jwt, args.balance, args.pin)
            accounts.append(acct)
            print(f"  [{i + 1}/{args.count}] {acct['accountNumber']} card={acct['cardNumber']}")
        except requests.HTTPError as e:
            print(f"  [{i + 1}/{args.count}] FAILED: {e} — {getattr(e.response, 'text', '')[:160]}",
                  file=sys.stderr)

    common.save_accounts(accounts)
    print(f"\nWrote {len(accounts)} account(s) to {common.ACCOUNTS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
