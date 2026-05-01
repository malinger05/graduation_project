import argparse
import getpass
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from secrets_manager import delete_secret, set_secret


def parse_args():
    parser = argparse.ArgumentParser(description="Manage ATM secrets in OS keychain.")
    parser.add_argument("action", choices=["set", "delete"], help="Action to perform")
    parser.add_argument(
        "name",
        choices=[
            "CONTRACT_ADDRESS",
            "ETH_PRIVATE_KEY",
            "DATABASE_URL",
            "ACCOUNTS_DATABASE_URL",
            "FLASK_SECRET_KEY",
        ],
        help="Secret name",
    )
    parser.add_argument("--value", help="Secret value for non-interactive set")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.action == "set":
        value = args.value or getpass.getpass(f"Enter value for {args.name}: ")
        set_secret(args.name, value)
        print(f"Stored {args.name} in keychain")
        return
    delete_secret(args.name)
    print(f"Deleted {args.name} from keychain (if present)")


if __name__ == "__main__":
    main()
