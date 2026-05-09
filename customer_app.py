"""
customer_app.py  —  Layer 1
Flask web UI. Talks ONLY to middleware (Layer 2).
No PostgreSQL. No blockchain. No Spring Boot calls.
"""
import os
import io
import base64
import threading
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
import qrcode
from secrets_manager import get_secret

load_dotenv()

from atm_architecture import (
    MIDDLEWARE_URL,
    ATMApp,
    AccountsRepository,
    TransactionsRepository,
)

app = Flask(__name__)
app.secret_key = get_secret("FLASK_SECRET_KEY", "change-me-set-FLASK_SECRET_KEY-in-env")

# Per-session ATM instances — each login gets its own middleware session token
_atm_sessions: dict[str, ATMApp] = {}
atm_ops_lock = threading.Lock()


def format_remaining_lock_time(total_seconds):
    seconds = max(0, int(total_seconds or 0))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def build_qr_data_uri(content):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def get_session_atm() -> ATMApp | None:
    key = session.get("atm_key")
    if not key:
        return None
    return _atm_sessions.get(key)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("account"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def _attach_qr(txn):
    """Build QR payload from a transaction and store in Flask session."""
    if not txn:
        return
    tx_hash = txn.get("blockchain_tx") or txn.get("blockchainTx")
    if not tx_hash:
        return
    verify_url = f"https://sepolia.etherscan.io/tx/{tx_hash}"
    qr_payload = {
        "type": txn.get("type", ""),
        "amount": float(txn.get("amount", 0) or 0),
        "verify_url": verify_url,
        "qr_data_uri": build_qr_data_uri(verify_url),
    }
    session["qr_popup"] = qr_payload
    session["last_qr"] = qr_payload


@app.route("/")
def index():
    if session.get("account"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", remaining_lock_seconds=0)

    account = (request.form.get("account") or "").strip()
    pin = (request.form.get("pin") or "").strip()
    if not account or not pin:
        flash("Enter account number and PIN.")
        return render_template("login.html", remaining_lock_seconds=0)

    try:
        # Fresh per-session repo and ATM instance
        accounts_repo = AccountsRepository(MIDDLEWARE_URL)
        auth_result = accounts_repo.authenticate_with_status(account, pin)
    except RuntimeError as e:
        return render_template("config_error.html", error=str(e)), 503
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    auth_status = auth_result.get("status")

    if auth_status == "locked":
        remaining_seconds = int(auth_result.get("remaining_lock_seconds", 0) or 0)
        remaining = format_remaining_lock_time(remaining_seconds)
        if auth_result.get("lock_minutes"):
            flash(
                f"Too many failed attempts. Account is locked for "
                f"{auth_result['lock_minutes']} minutes. Try again in {remaining}."
            )
        else:
            flash(f"Account is locked. Try again in {remaining}.")
        return render_template("login.html", remaining_lock_seconds=remaining_seconds)

    if auth_status != "ok":
        attempts_to_next_lock = auth_result.get("attempts_to_next_lock")
        if attempts_to_next_lock:
            flash(
                f"Invalid account number or PIN. "
                f"{attempts_to_next_lock} attempt(s) left before lockout."
            )
        else:
            flash("Invalid account number or PIN.")
        return render_template("login.html", remaining_lock_seconds=0)

    # Login successful — create per-session ATM instance
    import secrets as _s
    atm_key = _s.token_hex(8)
    transactions_repo = TransactionsRepository(accounts_repo)
    atm = ATMApp(accounts_repo, transactions_repo)
    atm.current_account = account
    _atm_sessions[atm_key] = atm

    user = auth_result["account"]
    session["account"] = account
    session["full_name"] = user.get("name", "Customer")
    session["user_id"] = user.get("account_id", account)
    session["atm_key"] = atm_key
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    key = session.get("atm_key")
    if key:
        _atm_sessions.pop(key, None)
    session.clear()
    flash("You are signed out.")
    return redirect(url_for("login"))


@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    atm = get_session_atm()
    if not atm:
        session.clear()
        return redirect(url_for("login"))

    try:
        balance = atm.check_balance()
        recent = [
            {
                "type": t.get("type", ""),
                "amount": float(t.get("amount", 0) or 0),
                "timestamp": str(t.get("created_at", "")),
                "status": t.get("status", "unknown"),
            }
            for t in atm.transactions_repo.get_transactions_for_account(session["account"], 10)
        ]
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    qr_popup = session.pop("qr_popup", None)
    last_qr = session.get("last_qr")
    return render_template(
        "dashboard.html",
        full_name=session.get("full_name", "Customer"),
        balance=balance,
        recent=recent,
        qr_popup=qr_popup,
        last_qr=last_qr,
    )


@app.route("/withdraw", methods=["POST"])
@login_required
def withdraw():
    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        flash("Enter a valid amount.")
        return redirect(url_for("dashboard"))

    atm = get_session_atm()
    if not atm:
        return redirect(url_for("login"))

    try:
        ok, msg, _ = atm.withdraw(amount)
        if ok:
            txn = atm.transactions_repo.get_latest_transaction_for_account(session["account"])
            _attach_qr(txn)
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    flash(msg)
    return redirect(url_for("dashboard"))


@app.route("/deposit", methods=["POST"])
@login_required
def deposit():
    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        flash("Enter a valid amount.")
        return redirect(url_for("dashboard"))

    atm = get_session_atm()
    if not atm:
        return redirect(url_for("login"))

    try:
        ok, msg, _ = atm.deposit(amount)
        if ok:
            txn = atm.transactions_repo.get_latest_transaction_for_account(session["account"])
            _attach_qr(txn)
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    flash(msg)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)