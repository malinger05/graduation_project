"""
Minimal web UI for ATM customers: login, balance, withdraw, deposit, recent activity.
Run: python customer_app.py   or   flask --app customer_app run
Run worker.py separately for background indexer tasks.
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
    ACCOUNTS_DATABASE_URL,
    ACCOUNTS_TABLE,
    DATABASE_URL,
    ATMApp,
    AccountsRepository,
    BlockchainGateway,
    Indexer,
    TransactionsRepository,
)

app = Flask(__name__)
app.secret_key = get_secret("FLASK_SECRET_KEY", "change-me-set-FLASK_SECRET_KEY-in-env")

_atm = None
_atm_lock = threading.Lock()
# Serialize operations on the shared ATM instance (simple local demo; not multi-tenant safe).
atm_ops_lock = threading.Lock()


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


def ensure_atm():
    global _atm
    with _atm_lock:
        if _atm is None:
            accounts_repo = AccountsRepository(ACCOUNTS_DATABASE_URL, ACCOUNTS_TABLE)
            transactions_repo = TransactionsRepository(DATABASE_URL)
            blockchain = BlockchainGateway()
            indexer = Indexer(transactions_repo, blockchain)
            _atm = ATMApp(accounts_repo, transactions_repo, blockchain, indexer)
        return _atm


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("account"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def bind_session_user(atm):
    acc = session["account"]
    user = atm.accounts_repo.get_account(acc)
    if not user:
        session.clear()
        flash("Session invalid. Please sign in again.")
        return redirect(url_for("login"))
    atm.current_account = acc
    session["full_name"] = user.get("name", session.get("full_name", "Customer"))
    return None


@app.route("/")
def index():
    if session.get("account"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    account = (request.form.get("account") or "").strip()
    pin = (request.form.get("pin") or "").strip()
    if not account or not pin:
        flash("Enter account number and PIN.")
        return render_template("login.html")

    try:
        with atm_ops_lock:
            atm = ensure_atm()
            user = atm.accounts_repo.authenticate(account, pin)
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    if not user:
        flash("Invalid account number or PIN.")
        return render_template("login.html")

    session["account"] = account
    session["full_name"] = user.get("name", "Customer")
    session["user_id"] = user.get("account_id", account)
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You are signed out.")
    return redirect(url_for("login"))


@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    try:
        with atm_ops_lock:
            atm = ensure_atm()
            redir = bind_session_user(atm)
            if redir:
                return redir
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
            recent.reverse()
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

    try:
        with atm_ops_lock:
            atm = ensure_atm()
            redir = bind_session_user(atm)
            if redir:
                return redir
            ok, msg, _on_chain = atm.withdraw(amount)
            if ok:
                txn = atm.transactions_repo.get_latest_transaction_for_account(session["account"])
                tx_hash = txn.get("blockchain_tx") if txn else None
                if tx_hash:
                    verify_url = f"https://sepolia.etherscan.io/tx/{tx_hash}"
                    qr_payload = {
                        "type": txn.get("type", ""),
                        "amount": float(txn.get("amount", 0) or 0),
                        "verify_url": verify_url,
                        "qr_data_uri": build_qr_data_uri(verify_url),
                    }
                    session["qr_popup"] = qr_payload
                    session["last_qr"] = qr_payload
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

    try:
        with atm_ops_lock:
            atm = ensure_atm()
            redir = bind_session_user(atm)
            if redir:
                return redir
            ok, msg, _on_chain = atm.deposit(amount)
            if ok:
                txn = atm.transactions_repo.get_latest_transaction_for_account(session["account"])
                tx_hash = txn.get("blockchain_tx") if txn else None
                if tx_hash:
                    verify_url = f"https://sepolia.etherscan.io/tx/{tx_hash}"
                    qr_payload = {
                        "type": txn.get("type", ""),
                        "amount": float(txn.get("amount", 0) or 0),
                        "verify_url": verify_url,
                        "qr_data_uri": build_qr_data_uri(verify_url),
                    }
                    session["qr_popup"] = qr_payload
                    session["last_qr"] = qr_payload
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    flash(msg)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
