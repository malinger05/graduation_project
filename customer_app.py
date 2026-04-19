"""
Minimal web UI for ATM customers: login, balance, withdraw, deposit, recent activity.
Run: python customer_app.py   or   flask --app customer_app run
Requires the same .env as atm.py (blockchain + Pinata).
"""
import os
import threading
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

load_dotenv()

from atm import ATMWithBlockchain, BlockchainLogger

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-set-FLASK_SECRET_KEY-in-env")

_atm = None
_atm_lock = threading.Lock()
# Serialize operations on the shared ATM instance (simple local demo; not multi-tenant safe).
atm_ops_lock = threading.Lock()


def ensure_atm():
    global _atm
    with _atm_lock:
        if _atm is None:
            _atm = ATMWithBlockchain(BlockchainLogger())
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
    user = atm.user_db.get_user_by_account(acc)
    if not user:
        session.clear()
        flash("Session invalid. Please sign in again.")
        return redirect(url_for("login"))
    atm.current_account = acc
    atm.current_user = dict(user)
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
            user = atm.user_db.verify_credentials(account, pin)
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    if not user:
        flash("Invalid account number or PIN.")
        return render_template("login.html")

    session["account"] = account
    session["full_name"] = user["full_name"]
    session["user_id"] = user["user_id"]
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
                t
                for t in atm.local_transactions
                if t.get("account") == session["account"]
            ][-10:]
            recent.reverse()
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    return render_template(
        "dashboard.html",
        full_name=session.get("full_name", "Customer"),
        balance=balance,
        recent=recent,
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
            _ok, msg, _on_chain = atm.withdraw(amount, generate_receipt=False)
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
            _ok, msg, _on_chain = atm.deposit(amount)
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    flash(msg)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
