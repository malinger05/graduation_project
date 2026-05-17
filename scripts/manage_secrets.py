import argparse
import getpass
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from secrets_manager import SENSITIVE_SECRET_NAMES, delete_secret, get_secret, set_secret

SECRET_NAMES = [
    # Sensitive — keychain only (no .env fallback)
    "CONTRACT_ADDRESS",
    "ETH_PRIVATE_KEY",
    "FLASK_SECRET_KEY",
    "MIDDLEWARE_SERVICE_TOKEN",
    # Middleware — keychain first, .env optional fallback
    "MIDDLEWARE_DB_URL",
    "CORE_BANKING_URL",
    "ETH_RPC_URL",
    "ETH_RPC_FALLBACK_URLS",
    "ACK_TIMEOUT_SECONDS",
    "SESSION_TTL_SECONDS",
    "ATM_SESSION_TTL_SECONDS",
    "ATM_IDLE_PROMPT_SECONDS",
    "ATM_PROMPT_TIMEOUT_SECONDS",
    "LOCKOUT_MAX_ATTEMPTS",
    "WORKER_RETRY_INTERVAL_SECONDS",
    "WORKER_CONFIRM_INTERVAL_SECONDS",
    "WORKER_TAMPER_INTERVAL_SECONDS",
    "WORKER_TAMPER_LOOKBACK_HOURS",
    "WORKER_MAX_SUBMIT_ATTEMPTS",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Manage ATM secrets in OS keychain.")
    parser.add_argument("action", choices=["set", "delete", "show"], help="Action to perform")
    parser.add_argument("name", choices=SECRET_NAMES, help="Secret / config name")
    parser.add_argument("--value", help="Secret value for non-interactive set")
    return parser.parse_args()


def _show(name: str) -> None:
    allow_env = name not in SENSITIVE_SECRET_NAMES
    value = get_secret(name, "", allow_env_fallback=allow_env)
    if not value:
        print(f"{name}: (not set in keychain" + ("" if not allow_env else " or .env") + ")")
        return
    if name in SENSITIVE_SECRET_NAMES:
        print(f"{name}: *** set ({len(value)} chars) ***")
    else:
        print(f"{name}: {value}")


def main():
    args = parse_args()
    if args.action == "show":
        _show(args.name)
        return
    if args.action == "set":
        value = args.value or getpass.getpass(f"Enter value for {args.name}: ")
        set_secret(args.name, value)
        print(f"Stored {args.name} in keychain")
        return
    delete_secret(args.name)
    print(f"Deleted {args.name} from keychain (if present)")


if __name__ == "__main__":
    main()
