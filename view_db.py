from datetime import datetime

from dotenv import load_dotenv

from secrets_manager import get_secret
from secure_user_db import DB_FILE, KEY_FILE, SecureUserDatabase

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError as exc:
    raise SystemExit("psycopg2 is required. Run: pip install psycopg2-binary") from exc


load_dotenv()

_DEFAULT_DB = "postgresql://localhost:5432/atm"
DATABASE_URL = get_secret("DATABASE_URL", _DEFAULT_DB).strip()


def format_cell(value):
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def print_table(title, rows, columns):
    print(f"\n{title}")
    print("-" * len(title))
    if not rows:
        print("(no rows)")
        return

    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            widths[col] = max(widths[col], len(format_cell(row.get(col))))

    header = " | ".join(col.ljust(widths[col]) for col in columns)
    sep = "-+-".join("-" * widths[col] for col in columns)
    print(header)
    print(sep)
    for row in rows:
        print(" | ".join(format_cell(row.get(col)).ljust(widths[col]) for col in columns))


def fetch_accounts():
    return SecureUserDatabase().list_users_public()


def fetch_transactions(limit=30):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, account_id, type, amount, status, blockchain_tx, block_number, created_at, confirmed_at
                FROM transactions
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def main():
    print("DB Viewer")
    print(f"Users (SQLite):  {DB_FILE}  (key: {KEY_FILE})")
    print(f"Transactions DB: {DATABASE_URL}")

    try:
        accounts = fetch_accounts()
        print_table("Accounts", accounts, ["account_id", "name", "balance"])
    except Exception as exc:
        print(f"\nCould not load accounts: {exc}")

    try:
        transactions = fetch_transactions(limit=20)
        print_table(
            "Latest Transactions (30)",
            transactions,
            [
                "id",
                "account_id",
                "type",
                "amount",
                "status",
                "blockchain_tx",
                "block_number",
                "created_at",
                "confirmed_at",
            ],
        )
    except Exception as exc:
        print(f"\nCould not load transactions: {exc}")


if __name__ == "__main__":
    main()
