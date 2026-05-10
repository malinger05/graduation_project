"""
Minimal web UI for ATM customers: login, balance, withdraw, deposit, recent activity.
Run: python customer_app.py   or   flask --app customer_app run
Run worker.py separately for background indexer tasks.
"""
import os
import io
import base64
import hmac
import logging
import re
import secrets
import threading
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
import qrcode
from werkzeug.middleware.proxy_fix import ProxyFix
from secrets_manager import get_secret

load_dotenv()

from atm_architecture import (
    DATABASE_URL,
    ATMApp,
    BlockchainGateway,
    Indexer,
    TransactionsRepository,
)
from secure_user_db import SecureUserDatabase

app = Flask(__name__)
app.secret_key = get_secret("FLASK_SECRET_KEY", "change-me-set-FLASK_SECRET_KEY-in-env")
trust_proxy = os.environ.get("TRUST_PROXY", "0").strip().lower() in {"1", "true", "yes"}
if trust_proxy:
    # Trust first reverse-proxy hop for protocol/host forwarding in production.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"},
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=20),
)
enable_hsts = os.environ.get("ENABLE_HSTS", "0").strip().lower() in {"1", "true", "yes"}
hsts_max_age = int(os.environ.get("HSTS_MAX_AGE_SECONDS", "31536000").strip())
logger = logging.getLogger(__name__)
ACCOUNT_RE = re.compile(r"^\d{4,16}$")
PIN_RE = re.compile(r"^\d{4,8}$")
MAX_TX_AMOUNT = Decimal("10000.00")

_atm = None
_atm_lock = threading.Lock()
# Serialize operations on the shared ATM instance (simple local demo; not multi-tenant safe).
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


def get_or_create_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": get_or_create_csrf_token()}


@app.before_request
def protect_post_routes():
    if request.method != "POST":
        return None
    submitted = request.form.get("csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not expected or not submitted or not hmac.compare_digest(submitted, expected):
        flash("Security check failed. Please retry.")
        return redirect(url_for("login"))
    return None


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    if enable_hsts and request.is_secure:
        response.headers["Strict-Transport-Security"] = f"max-age={hsts_max_age}; includeSubDomains"
    return response


def is_valid_account(account):
    return bool(ACCOUNT_RE.fullmatch(account or ""))


def is_valid_pin(pin):
    return bool(PIN_RE.fullmatch(pin or ""))


def parse_amount(raw_amount):
    try:
        amount = Decimal(str(raw_amount or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    if amount <= Decimal("0") or amount > MAX_TX_AMOUNT:
        return None
    return float(amount)


def config_error_response(exc):
    logger.exception("App error: %s", exc)
    return render_template("config_error.html", error="Service temporarily unavailable."), 503


def ensure_atm():
    global _atm
    with _atm_lock:
        if _atm is None:
            accounts_repo = SecureUserDatabase()
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
        return render_template("login.html", remaining_lock_seconds=0)

    account = (request.form.get("account") or "").strip()
    pin = (request.form.get("pin") or "").strip()
    if not account or not pin:
        flash("Enter account number and PIN.")
        return render_template("login.html", remaining_lock_seconds=0)
    if not is_valid_account(account) or not is_valid_pin(pin):
        flash("Invalid account format or PIN format.")
        return render_template("login.html", remaining_lock_seconds=0)

    try:
        with atm_ops_lock:
            atm = ensure_atm()
            auth_result = atm.accounts_repo.authenticate_with_status(account, pin)
    except Exception as e:
        return config_error_response(e)

    auth_status = auth_result.get("status")
    if auth_status == "locked":
        remaining_seconds = int(auth_result.get("remaining_lock_seconds", 0) or 0)
        remaining = format_remaining_lock_time(remaining_seconds)
        if auth_result.get("lock_minutes"):
            flash(
                f"Too many failed attempts. Account is locked for {auth_result['lock_minutes']} minutes. "
                f"Try again in {remaining}."
            )
        else:
            flash(f"Account is locked. Try again in {remaining}.")
        return render_template("login.html", remaining_lock_seconds=remaining_seconds)

    if auth_status != "ok":
        attempts_to_next_lock = auth_result.get("attempts_to_next_lock")
        if attempts_to_next_lock:
            flash(
                f"Invalid account number or PIN. {attempts_to_next_lock} attempt(s) left before lockout."
            )
        else:
            flash("Invalid account number or PIN.")
        return render_template("login.html", remaining_lock_seconds=0)

    user = auth_result["account"]
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
        return config_error_response(e)

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
    amount = parse_amount(request.form.get("amount"))
    if amount is None:
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
        return config_error_response(e)

    flash(msg)
    return redirect(url_for("dashboard"))


@app.route("/deposit", methods=["POST"])
@login_required
def deposit():
    amount = parse_amount(request.form.get("amount"))
    if amount is None:
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
        return config_error_response(e)

    flash(msg)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
