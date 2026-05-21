#!/usr/bin/env python3
"""
Instant lockout states for manual testing (no waiting for 5/15/30 min timers).

Requires MIDDLEWARE_DB_URL (same keychain / .env as middleware).
Does not work when middleware uses in-memory lockouts only.

Usage (from repo root):
  python3 scripts/lockout_dev.py show ACC53C01C3F9CFF
  python3 scripts/lockout_dev.py permanent ACC53C01C3F9CFF
  python3 scripts/lockout_dev.py must-reset ACC53C01C3F9CFF   # after admin unlock
  python3 scripts/lockout_dev.py timed ACC53C01C3F9CFF 30     # locked 30 seconds
  python3 scripts/lockout_dev.py clear ACC53C01C3F9CFF
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MW = os.path.join(ROOT, "atm-middleware")
for p in (ROOT, MW):
    if p not in sys.path:
        sys.path.insert(0, p)

import db  # noqa: E402
import lockouts  # noqa: E402
from models import LoginLockout  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_or_create(session, account: str) -> LoginLockout:
    row = session.get(LoginLockout, account)
    if row is None:
        row = LoginLockout(
            account_number=account,
            failed_attempts=0,
            lock_tier=0,
            locked_until=None,
            permanently_locked=False,
            must_reset_pin=False,
            updated_at=_now(),
        )
        session.add(row)
        session.flush()
    return row


def cmd_show(account: str) -> None:
    with db.db_session() as s:
        row = s.get(LoginLockout, account)
        if row is None:
            print(f"{account}: no lockout row")
            return
        print(
            f"{account}: failed={row.failed_attempts} tier={row.lock_tier} "
            f"locked_until={row.locked_until} permanent={row.permanently_locked} "
            f"must_reset_pin={row.must_reset_pin}"
        )


def cmd_permanent(account: str) -> None:
    with db.db_session() as s:
        row = _get_or_create(s, account)
        row.failed_attempts = 12
        row.lock_tier = 99
        row.locked_until = None
        row.permanently_locked = True
        row.must_reset_pin = False
        row.updated_at = _now()
    print(f"{account}: permanently_locked=true (contact admin at ATM)")


def cmd_must_reset(account: str) -> None:
    lockouts.admin_unlock(account)
    print(f"{account}: admin unlock applied — must_reset_pin=true (use Set new PIN at ATM)")


def cmd_timed(account: str, seconds: int) -> None:
    until = _now() + timedelta(seconds=seconds)
    with db.db_session() as s:
        row = _get_or_create(s, account)
        row.failed_attempts = 3
        row.lock_tier = 1
        row.locked_until = until
        row.permanently_locked = False
        row.must_reset_pin = False
        row.updated_at = _now()
    print(f"{account}: timed lock for {seconds}s (until {until.isoformat()})")


def cmd_clear(account: str) -> None:
    with db.db_session() as s:
        row = s.get(LoginLockout, account)
        if row is None:
            print(f"{account}: already clear")
            return
        s.delete(row)
    print(f"{account}: lockout row deleted")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["show", "permanent", "must-reset", "timed", "clear"],
    )
    parser.add_argument("account", help="Account number, e.g. ACC53C01C3F9CFF")
    parser.add_argument(
        "seconds",
        nargs="?",
        type=int,
        default=30,
        help="For timed: lock duration in seconds (default 30)",
    )
    args = parser.parse_args()

    if not db.init_db():
        print(
            "MIDDLEWARE_DB_URL is not set. Lockout dev tools need the middleware Postgres.\n"
            "  python3 scripts/manage_secrets.py set MIDDLEWARE_DB_URL\n"
            "Or enable fast timers without DB: LOCKOUT_FAST_TEST=1 then restart middleware."
        )
        sys.exit(1)

    import models  # noqa: F401

    actions = {
        "show": lambda: cmd_show(args.account),
        "permanent": lambda: cmd_permanent(args.account),
        "must-reset": lambda: cmd_must_reset(args.account),
        "timed": lambda: cmd_timed(args.account, args.seconds),
        "clear": lambda: cmd_clear(args.account),
    }
    actions[args.action]()


if __name__ == "__main__":
    main()
