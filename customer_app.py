"""
customer_app.py  —  Layer 1
Flask web UI. Talks only to middleware (Layer 2).
No blockchain. No PostgreSQL. No Spring Boot calls.
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

from atm_architecture import ATMApp, make_accounts_repo

app = Flask(__name__)
app.secret_key = get_secret("FLASK_SECRET_KEY", "change-me")

_atm_lock = threading.Lock()
_atm: ATMApp | None = None


def build_qr_data_uri(content: str) -> str:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L,
                        box_size=8, border=2)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def get_atm() -> ATMApp:
    global _atm
    with _atm_lock:
        if _atm is None:
            _atm = ATMApp(make_accounts_repo())
    return _atm


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("account"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    return redirect(url_for("dashboard") if session.get("account") else url_for("login"))


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
        # Fresh ATM instance per login so session token is isolated
        atm = ATMApp(make_accounts_repo())
        success = atm.authenticate(account, pin)
    except RuntimeError as e:
        return render_template("config_error.html", error=str(e)), 503
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    if not success:
        flash("Invalid account number or PIN.")
        return render_template("login.html")

    # Store ATM instance in a thread-local way via session ID
    import secrets as _s
    atm_key = _s.token_hex(8)
    _atm_sessions[atm_key] = atm

    session["account"] = account
    session["full_name"] = atm.accounts_repo.get_account(account).get("name", "Customer")
    session["atm_key"] = atm_key
    return redirect(url_for("dashboard"))


# Per-session ATM instances (each has its own middleware session token)
_atm_sessions: dict[str, ATMApp] = {}


def get_session_atm() -> ATMApp | None:
    key = session.get("atm_key")
    if not key:
        return None
    return _atm_sessions.get(key)


@app.route("/logout", methods=["POST"])
def logout():
    key = session.get("atm_key")
    if key:
        _atm_sessions.pop(key, None)
    session.clear()
    flash("You are signed out.")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    atm = get_session_atm()
    if not atm:
        session.clear()
        return redirect(url_for("login"))

    try:
        balance = atm.check_balance()
        recent = atm.get_transactions()
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    qr_popup = session.pop("qr_popup", None)
    last_qr = session.get("last_qr")

    return render_template(
        "dashboard.html",
        full_name=session.get("full_name", "Customer"),
        balance=balance,
        recent=[
            {
                "type": t.get("type", ""),
                "amount": float(t.get("amount", 0) or 0),
                "timestamp": str(t.get("created_at", "")),
            }
            for t in (recent or [])
        ],
        qr_popup=qr_popup,
        last_qr=last_qr,
    )


@app.route("/withdraw", methods=["POST"])
@login_required
def withdraw():
    atm = get_session_atm()
    if not atm:
        return redirect(url_for("login"))

    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        flash("Enter a valid amount.")
        return redirect(url_for("dashboard"))

    try:
        ok, msg, result = atm.withdraw(amount)
        if ok and result:
            verify_url = result.get("verifyUrl")
            if verify_url:
                qr_payload = {
                    "type": "WITHDRAW",
                    "amount": amount,
                    "verify_url": verify_url,
                    "qr_data_uri": build_qr_data_uri(verify_url),
                }
                session["qr_popup"] = qr_payload
                session["last_qr"] = qr_payload
    except Exception as e:
        flash(str(e))
        return redirect(url_for("dashboard"))

    flash(msg)
    return redirect(url_for("dashboard"))


@app.route("/deposit", methods=["POST"])
@login_required
def deposit():
    atm = get_session_atm()
    if not atm:
        return redirect(url_for("login"))

    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        flash("Enter a valid amount.")
        return redirect(url_for("dashboard"))

    try:
        ok, msg, result = atm.deposit(amount)
        if ok and result:
            verify_url = result.get("verifyUrl")
            if verify_url:
                qr_payload = {
                    "type": "DEPOSIT",
                    "amount": amount,
                    "verify_url": verify_url,
                    "qr_data_uri": build_qr_data_uri(verify_url),
                }
                session["qr_popup"] = qr_payload
                session["last_qr"] = qr_payload
    except Exception as e:
        flash(str(e))
        return redirect(url_for("dashboard"))

    flash(msg)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)