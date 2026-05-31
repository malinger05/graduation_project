"""
customer_app.py  —  Layer 1
Flask web UI. Talks ONLY to middleware (Layer 2).
No PostgreSQL. No blockchain. No Spring Boot calls.
"""
import os
import io
import base64
import secrets
import threading
import time
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for, jsonify
import qrcode
from secrets_manager import get_secret
from security_headers import (
    configure_session_cookies,
    register_security_headers,
    suppress_werkzeug_server_version,
)

load_dotenv()

import mw_http
from atm_architecture import (
    MIDDLEWARE_URL,
    ATMApp,
    AccountsRepository,
    TransactionsRepository,
)

app = Flask(__name__)
app.secret_key = get_secret("FLASK_SECRET_KEY", "change-me-set-FLASK_SECRET_KEY-in-env")
app.config["SESSION_COOKIE_NAME"] = "atm_session"
app.config["SESSION_COOKIE_PATH"] = "/"
configure_session_cookies(app)
register_security_headers(app)


@app.context_processor
def inject_idle_session_config():
    return {
        "atm_idle_prompt_ms": ATM_IDLE_PROMPT_SECONDS * 1000,
        "atm_prompt_timeout_ms": ATM_PROMPT_TIMEOUT_SECONDS * 1000,
    }


ATM_SESSION_TTL_SECONDS = int(
    get_secret("ATM_SESSION_TTL_SECONDS", "900", allow_env_fallback=False)
)
ATM_IDLE_PROMPT_SECONDS = int(
    get_secret("ATM_IDLE_PROMPT_SECONDS", "120", allow_env_fallback=False)
)
ATM_PROMPT_TIMEOUT_SECONDS = int(
    get_secret("ATM_PROMPT_TIMEOUT_SECONDS", "60", allow_env_fallback=False)
)

_atm_sessions: dict[str, dict] = {}
_atm_sessions_lock = threading.Lock()


def _evict_atm_session(atm_key: str) -> None:
    with _atm_sessions_lock:
        entry = _atm_sessions.pop(atm_key, None)
    if entry:
        try:
            atm: ATMApp = entry["atm"]
            client = atm.accounts_repo.client
            if client._session_token:
                mw_http.post(
                    f"{client.base_url}/atm/logout",
                    headers={"x-session-token": client._session_token},
                    timeout=60,
                )
        except Exception:
            pass


def _session_cleanup() -> None:
    while True:
        time.sleep(60)
        cutoff = time.time() - ATM_SESSION_TTL_SECONDS
        with _atm_sessions_lock:
            stale_keys = [
                k for k, v in _atm_sessions.items()
                if v.get("last_active", 0) < cutoff
            ]
        for key in stale_keys:
            _evict_atm_session(key)
        if stale_keys:
            app.logger.info(f"[SessionCleanup] Evicted {len(stale_keys)} idle ATM session(s)")


_cleanup_thread = threading.Thread(target=_session_cleanup, daemon=True)
_cleanup_thread.start()


def _get_session_atm() -> ATMApp | None:
    key = session.get("atm_key")
    if not key:
        return None
    with _atm_sessions_lock:
        entry = _atm_sessions.get(key)
        if entry:
            entry["last_active"] = time.time()
            return entry["atm"]
    return None


def _register_atm_session(atm_key: str, atm: ATMApp) -> None:
    with _atm_sessions_lock:
        _atm_sessions[atm_key] = {"atm": atm, "last_active": time.time()}


def _new_idempotency_key() -> str:
    return secrets.token_hex(16)


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


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("card_number"):   # CHANGED from "account"
            return redirect(url_for("atm_home"))
        return view(*args, **kwargs)
    return wrapped


def _build_qr_payload(txn) -> dict | None:
    if not txn:
        return None
    tx_hash = txn.get("blockchain_tx") or txn.get("blockchainTx")
    if not tx_hash:
        return None
    verify_url = (
        txn.get("verify_url")
        or txn.get("verifyUrl")
        or f"https://sepolia.etherscan.io/tx/{tx_hash}"
    )
    return {
        "type": txn.get("type", ""),
        "amount": float(txn.get("amount", 0) or 0),
        "verify_url": verify_url,
        "qr_data_uri": build_qr_data_uri(verify_url),
    }


def _attach_qr(txn) -> dict | None:
    qr_payload = _build_qr_payload(txn)
    if qr_payload:
        session["qr_popup"] = qr_payload
        session["last_qr"] = qr_payload
    return qr_payload


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("atm_home"))


@app.route("/atm")
def atm_home():
    """
    Single-page ATM shell. If logged in, passes balance/name so JS
    can skip straight to the menu. If not logged in, shows idle/login screen.
    """
    if session.get("card_number"):   # CHANGED from "account"
        atm = _get_session_atm()
        if not atm:
            session.clear()
            return render_template("atm.html",
                logged_in=False,
                full_name="",
                balance=0,
                account="",
                last_qr=None,
            )
        try:
            balance = atm.check_balance()
        except Exception:
            balance = 0
        return render_template("atm.html",
            logged_in=True,
            full_name=session.get("full_name", "Customer"),
            balance=balance,
            account=session.get("account", ""),      # account number still used for display
            last_qr=session.get("last_qr"),
        )
    return render_template("atm.html",
        logged_in=False,
        full_name="",
        balance=0,
        account="",
        last_qr=None,
    )


# Keep /dashboard pointing here for any redirects
@app.route("/dashboard")
def dashboard():
    return redirect(url_for("atm_home"))


@app.route("/check-card", methods=["POST"])
@app.route("/check-account", methods=["POST"])  # frontend compatibility
def check_card():
    card_number = (request.form.get("card_number") or request.form.get("account") or "").strip()
    if not card_number:
        return jsonify({"status": "error", "message": "Enter card number."}), 400

    try:
        repo = AccountsRepository(MIDDLEWARE_URL)
        result = repo.check_account_status(card_number)   # passes card_number as key
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    status = result.get("status", "ok")
    if status == "pin_reset_required":
        return jsonify({
            "status":      "pin_reset_required",
            "card_number": card_number,
            "message":     "Your card was unlocked by the bank. You must set a new PIN before logging in.",
        })
    if status == "locked":
        if result.get("admin_unlock_required"):
            return jsonify({
                "status":               "locked",
                "admin_unlock_required": True,
                "remaining_lock_seconds": 0,
                "message":              "Card locked. Contact an administrator to unlock.",
            }), 403
        remaining = int(result.get("remaining_lock_seconds", 300))
        mins, secs = divmod(remaining, 60)
        return jsonify({
            "status":                 "locked",
            "remaining_lock_seconds": remaining,
            "message":                f"Card locked. Try again in {mins:02d}:{secs:02d}.",
        }), 403

    return jsonify({"status": "ok", "card_number": card_number})


@app.route("/login", methods=["POST"])
def login():
    card_number = (request.form.get("card_number") or request.form.get("account") or "").strip()
    pin         = (request.form.get("pin") or "").strip()

    if not card_number or not pin:
        return jsonify({"status": "error", "message": "Enter card number and PIN."}), 400

    try:
        accounts_repo = AccountsRepository(MIDDLEWARE_URL)
        auth_result   = accounts_repo.authenticate_with_status(card_number, pin)  # CHANGED
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    auth_status = auth_result.get("status")

    if auth_status == "pin_reset_required":
        return jsonify({
            "status":      "pin_reset_required",
            "card_number": card_number,
            "message":     "Your card was unlocked by the bank. Please set a new PIN.",
        }), 403

    if auth_status == "locked":
        if auth_result.get("admin_unlock_required"):
            return jsonify({
                "status":               "locked",
                "admin_unlock_required": True,
                "remaining_lock_seconds": 0,
                "message":              "Card locked. Contact an administrator to unlock.",
            }), 403
        remaining = int(auth_result.get("remaining_lock_seconds", 300))
        mins, secs = divmod(remaining, 60)
        return jsonify({
            "status":                 "locked",
            "remaining_lock_seconds": remaining,
            "message":                f"Card locked. Try again in {mins:02d}:{secs:02d}.",
        }), 403

    if auth_status != "ok":
        attempts = auth_result.get("attempts_to_next_lock")
        msg = (
            f"Invalid credentials. {attempts} attempt(s) left before lockout."
            if attempts
            else "Invalid card number or PIN."
        )
        return jsonify({"status": "invalid", "message": msg}), 401

    # Success — create session
    import secrets as _s
    atm_key           = _s.token_hex(8)
    transactions_repo = TransactionsRepository(accounts_repo)
    atm               = ATMApp(accounts_repo, transactions_repo)
    atm.current_account = auth_result.get("accountNumber", card_number)
    _register_atm_session(atm_key, atm)

    user = auth_result.get("account", {})
    session["card_number"] = card_number                              # CHANGED — primary session key
    session["account"]     = auth_result.get("accountNumber", "")    # resolved account number for display
    session["full_name"]   = user.get("name", "Customer")
    session["user_id"]     = user.get("account_id", "")
    session["atm_key"]     = atm_key

    return jsonify({
        "status":      "ok",
        "full_name":   user.get("name", "Customer"),
        "balance":     float(user.get("balance", 0)),
        "card_number": card_number,
        "account":     auth_result.get("accountNumber", ""),
    })


@app.route("/card-setup", methods=["GET", "POST"])
def card_setup():
    """
    Three-step wizard:
      step=account  → customer enters IBAN account number
      step=pin      → customer sets PIN twice
      step=done     → success screen
    """
    if request.method == "GET":
        return render_template("card_setup.html", step="account")
 
    step = request.form.get("step", "account")
 
    # ── Step 1: customer submits account number ────────────────────────────────
    if step == "account":
        account_number = (request.form.get("account_number") or "").strip().upper()
        card_number = (request.form.get("card_number") or "").strip().replace(" ", "")
 
        if not account_number:
            flash("Please enter your account number.")
            return render_template("card_setup.html", step="account")
        if not card_number:
            flash("Please enter your card number from the email.")
            return render_template("card_setup.html", step="account")
 
        # Basic IBAN format check (DE + 20 digits = 22 chars)
        if not account_number.startswith("DE") or len(account_number) != 22 or not account_number[2:].isdigit():
            flash("Invalid account number format. It should start with DE followed by 20 digits.")
            return render_template("card_setup.html", step="account")
        if not card_number.isdigit() or len(card_number) != 16:
            flash("Card number must be exactly 16 digits.")
            return render_template("card_setup.html", step="account")
 
        # Validate existing card and prepare PIN setup (no card creation here).
        try:
            resp = mw_http.post(
                f"{MIDDLEWARE_URL}/atm/prepare-own-pin",
                json={"accountNumber": account_number, "cardNumber": card_number},
                timeout=(5, 15),
            )
        except Exception as e:
            flash("Cannot reach the banking system. Please try again.")
            return render_template("card_setup.html", step="account")
 
        if resp.status_code == 404:
            flash("Card or account not found. Please check the details sent by the bank.")
            return render_template("card_setup.html", step="account")
 
        if not resp.ok:
            try:
                detail = resp.json().get("error") or resp.json().get("detail") or resp.text
            except Exception:
                detail = resp.text
            flash(f"Card verification failed: {detail}")
            return render_template("card_setup.html", step="account")
 
        data = resp.json()
        card_id     = data.get("cardId")
        masked_card = data.get("maskedNumber", "**** **** **** ????")
 
        return render_template(
            "card_setup.html",
            step="pin",
            card_id=card_id,
            masked_card=masked_card,
            account_number=account_number,
            had_existing_cards=False,
        )
 
    # ── Step 2: customer submits PIN ──────────────────────────────────────────
    if step == "pin":
        card_id     = request.form.get("card_id", "")
        account_number = (request.form.get("account_number") or "").strip().upper()
        pin         = request.form.get("pin", "").strip()
        pin_confirm = request.form.get("pin_confirm", "").strip()
        had_existing_cards = request.form.get("had_existing_cards") == "1"
 
        if not card_id:
            flash("Session lost. Please start again.")
            return render_template("card_setup.html", step="account")

        if not account_number.startswith("DE") or len(account_number) != 22 or not account_number[2:].isdigit():
            flash("Please enter the same valid account number to confirm PIN setup.")
            return render_template(
                "card_setup.html",
                step="pin",
                card_id=card_id,
                masked_card=request.form.get("masked_card", ""),
                account_number=account_number,
                had_existing_cards=had_existing_cards,
            )
 
        # Client-side JS already checks this, but validate server-side too
        if pin != pin_confirm:
            flash("PINs do not match. Please try again.")
            return render_template(
                "card_setup.html",
                step="pin",
                card_id=card_id,
                masked_card=request.form.get("masked_card", ""),
                account_number=account_number,
                had_existing_cards=had_existing_cards,
            )
 
        if not pin.isdigit() or len(pin) != 4:
            flash("PIN must be exactly 4 digits.")
            return render_template(
                "card_setup.html",
                step="pin",
                card_id=card_id,
                masked_card=request.form.get("masked_card", ""),
                account_number=account_number,
                had_existing_cards=had_existing_cards,
            )
 
        try:
            resp = mw_http.post(
                f"{MIDDLEWARE_URL}/atm/set-own-pin",
                json={
                    "cardId": int(card_id),
                    "accountNumber": account_number,
                    "pin": pin,
                    "pinConfirm": pin_confirm,
                },
                timeout=(5, 15),
            )
        except Exception:
            flash("Cannot reach the banking system. Please try again.")
            return render_template(
                "card_setup.html",
                step="pin",
                card_id=card_id,
                masked_card=request.form.get("masked_card", ""),
                account_number=account_number,
                had_existing_cards=had_existing_cards,
            )
 
        if not resp.ok:
            try:
                detail = resp.json().get("error") or resp.json().get("detail") or resp.text
            except Exception:
                detail = resp.text
            flash(f"PIN setup failed: {detail}")
            return render_template(
                "card_setup.html",
                step="pin",
                card_id=card_id,
                masked_card=request.form.get("masked_card", ""),
                account_number=account_number,
                had_existing_cards=had_existing_cards,
            )
 
        data = resp.json()
        masked_card = data.get("maskedNumber", "**** **** **** ????")
 
        return render_template("card_setup.html", step="done", masked_card=masked_card)
 
    # Fallback — unknown step
    return redirect(url_for("card_setup"))

"""
customer_app.py — ADD these two routes.

These serve the fetch() calls from the SPA in atm.html.
They do NOT render templates — they return JSON.

Also make sure `from flask import jsonify` is imported (it already is in the original).
"""

from flask import jsonify, request
import mw_http
from atm_architecture import MIDDLEWARE_URL


@app.route("/card-setup/create", methods=["POST"])
def card_setup_create():
    """
    Step 1 of self-service card setup.
    ATM SPA POSTs { accountNumber } here.
    We forward to middleware → Core Banking to create the card.
    Returns JSON: { cardId, maskedNumber, accountNumber } or { error }
    """
    data           = request.get_json(silent=True) or {}
    account_number = (data.get("accountNumber") or "").strip().upper()

    if not account_number:
        return jsonify({"error": "accountNumber is required"}), 400

    # Basic format guard (DE + 20 digits)
    import re
    if not re.match(r"^DE\d{20}$", account_number):
        return jsonify({"error": "Invalid account number format. Must be DE followed by 20 digits."}), 400

    try:
        resp = mw_http.post(
            f"{MIDDLEWARE_URL}/atm/prepare-own-pin",
            json={"accountNumber": account_number},
            timeout=(5, 15),
        )
    except Exception:
        return jsonify({"error": "Cannot reach banking system. Please try again."}), 503

    try:
        body = resp.json()
    except Exception:
        body = {}

    if not resp.ok:
        error_msg = body.get("error") or body.get("detail") or "Card verification failed."
        return jsonify({"error": error_msg}), resp.status_code

    return jsonify(body), 201


@app.route("/card-setup/set-pin", methods=["POST"])
def card_setup_set_pin():
    """
    Step 2 of self-service card setup.
    ATM SPA POSTs { cardId, accountNumber, pin, pinConfirm } here.
    We validate match and format, then forward to middleware → Core Banking.
    Returns JSON: { maskedNumber, message } or { error }
    """
    data       = request.get_json(silent=True) or {}
    card_id    = data.get("cardId")
    account_number = (data.get("accountNumber") or "").strip().upper()
    pin        = str(data.get("pin") or "").strip()
    pin_confirm = str(data.get("pinConfirm") or "").strip()

    if not card_id or not account_number or not pin or not pin_confirm:
        return jsonify({"error": "cardId, accountNumber, pin, and pinConfirm are required"}), 400

    import re
    if not re.match(r"^DE\d{20}$", account_number):
        return jsonify({"error": "Invalid account number format. Must be DE followed by 20 digits."}), 400

    if pin != pin_confirm:
        return jsonify({"error": "PINs do not match."}), 400

    if not re.match(r"^\d{4}$", pin):
        return jsonify({"error": "PIN must be exactly 4 digits."}), 400

    try:
        resp = mw_http.post(
            f"{MIDDLEWARE_URL}/atm/set-own-pin",
            json={
                "cardId": int(card_id),
                "accountNumber": account_number,
                "pin": pin,
                "pinConfirm": pin_confirm,
            },
            timeout=(5, 15),
        )
    except Exception:
        return jsonify({"error": "Cannot reach banking system. Please try again."}), 503

    try:
        body = resp.json()
    except Exception:
        body = {}

    if not resp.ok:
        error_msg = body.get("error") or body.get("detail") or "PIN setup failed."
        return jsonify({"error": error_msg}), resp.status_code

    return jsonify(body), 200


@app.route("/reset-pin", methods=["POST"])
def reset_pin():
    card_number  = (request.form.get("card_number") or request.form.get("account") or "").strip()
    new_pin      = (request.form.get("newPin") or "").strip()
    confirm_pin  = (request.form.get("confirmPin") or "").strip()

    if not card_number or not new_pin or not confirm_pin:
        return jsonify({"status": "error", "message": "Enter card number and PIN twice."}), 400

    try:
        repo   = AccountsRepository(MIDDLEWARE_URL)
        result = repo.reset_pin(card_number, new_pin, confirm_pin)   # CHANGED
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    if result.get("status") != "ok":
        return jsonify({
            "status":  "error",
            "message": result.get("message", "Could not reset PIN."),
        }), 400

    return jsonify({
        "status":  "ok",
        "message": result.get("message", "PIN updated. Please log in with your new PIN."),
    })


@app.route("/logout", methods=["POST"])
def logout():
    key = session.get("atm_key")
    if key:
        _evict_atm_session(key)
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/session/continue", methods=["POST"])
@login_required
def session_continue():
    atm = _get_session_atm()
    if not atm:
        return jsonify({"ok": False}), 401
    token = atm.accounts_repo.client._session_token
    if token:
        try:
            resp = mw_http.post(
                f"{MIDDLEWARE_URL}/atm/session/continue",
                headers={"x-session-token": token},
                timeout=60,
            )
            if resp.status_code == 401:
                session.clear()
                return jsonify({"ok": False}), 401
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/withdraw", methods=["POST"])
@login_required
def withdraw():
    data = request.get_json(silent=True) or request.form
    idempotency_key = (data.get("idempotency_key") or "").strip()
    if not idempotency_key:
        idempotency_key = _new_idempotency_key()

    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid amount."}), 400

    atm = _get_session_atm()
    if not atm:
        return jsonify({"status": "error", "message": "Session expired."}), 401

    try:
        ok, msg, result = atm.accounts_repo.client.withdraw(
            amount, idempotency_key=idempotency_key
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    if ok and isinstance(result, dict):
        qr = _attach_qr({
            "type":         "WITHDRAW",
            "amount":       amount,
            "blockchain_tx": result.get("blockchainTx"),
            "verify_url":   result.get("verifyUrl"),
        })
        return jsonify({
            "status":        "ok",
            "message":       msg,
            "newBalance":    result.get("newBalance", 0),
            "blockchainTx":  result.get("blockchainTx", ""),
            "middlewareTxId": result.get("middlewareTxId") or result.get("transactionId"),
            "transactionId": result.get("transactionId"),
            "qr":            qr,
        })

    return jsonify({"status": "error", "message": msg}), 400


@app.route("/ack", methods=["POST"])
@login_required
def ack_dispense():
    data = request.get_json(silent=True) or request.form
    try:
        middleware_tx_id = int(data.get("middlewareTxId") or data.get("transactionId") or 0)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid transaction id."}), 400
    if middleware_tx_id <= 0:
        return jsonify({"status": "error", "message": "Transaction id required."}), 400

    atm = _get_session_atm()
    if not atm:
        return jsonify({"status": "error", "message": "Session expired."}), 401

    ok, msg = atm.accounts_repo.client.confirm_dispense(middleware_tx_id)
    if ok:
        return jsonify({"status": "ok", "message": msg, "middlewareTxId": middleware_tx_id})
    return jsonify({"status": "error", "message": msg}), 400


@app.route("/deposit", methods=["POST"])
@login_required
def deposit():
    data = request.get_json(silent=True) or request.form
    idempotency_key = (data.get("idempotency_key") or "").strip()
    if not idempotency_key:
        idempotency_key = _new_idempotency_key()

    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid amount."}), 400

    atm = _get_session_atm()
    if not atm:
        return jsonify({"status": "error", "message": "Session expired."}), 401

    try:
        ok, msg, result = atm.accounts_repo.client.deposit(
            amount, idempotency_key=idempotency_key
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    if ok and isinstance(result, dict):
        qr = _attach_qr({
            "type":         "DEPOSIT",
            "amount":       amount,
            "blockchain_tx": result.get("blockchainTx"),
            "verify_url":   result.get("verifyUrl"),
        })
        return jsonify({
            "status":       "ok",
            "message":      msg,
            "newBalance":   result.get("newBalance", 0),
            "blockchainTx": result.get("blockchainTx", ""),
            "qr":           qr,
        })

    return jsonify({"status": "error", "message": msg}), 400


@app.route("/balance-api")
@login_required
def balance_api():
    atm = _get_session_atm()
    if not atm:
        return jsonify({"status": "error"}), 401
    try:
        balance = atm.check_balance()
        return jsonify({"status": "ok", "balance": balance})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503


@app.route("/transactions-api")
@login_required
def transactions_api():
    atm = _get_session_atm()
    if not atm:
        return jsonify({"status": "error"}), 401
    try:
        raw = atm.transactions_repo.get_transactions_for_account(session["account"])
        recent = [
            {
                "type":           t.get("transactionType") or t.get("type", ""),
                "amount":         float(t.get("amount", 0) or 0),
                "timestamp":      str(t.get("createdAt") or t.get("created_at", "")),
                "status":         t.get("transactionStatus") or t.get("status", "APPROVED"),
                "transaction_id": t.get("transactionId"),
                "chain_status":   t.get("chainStatus", "PENDING_SUBMIT"),
                "blockchain_tx":  t.get("blockchainTx", ""),
            }
            for t in raw
        ]
        return jsonify({"status": "ok", "transactions": recent})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503


@app.route("/tx-status/<int:transaction_id>")
@login_required
def tx_status(transaction_id):
    atm = _get_session_atm()
    if not atm:
        return jsonify({"error": "no session"}), 401
    try:
        resp = mw_http.get(
            f"{MIDDLEWARE_URL}/atm/tx-status/{transaction_id}",
            headers={"x-session-token": atm.accounts_repo.client._session_token},
            timeout=60,
        )
        return resp.json(), resp.status_code
    except Exception:
        return jsonify({"error": "unavailable"}), 503


if __name__ == "__main__":
    suppress_werkzeug_server_version()
    port = int(os.environ.get("PORT", "5001"))
    # Localhost only — use Caddy https://atm.local in the browser (see scripts/caddy/).
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    print(f"[customer_app] http://{host}:{port}  (browser: https://atm.local via Caddy)")
    app.run(host=host, port=port, debug=False)