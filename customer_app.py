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
import requests as _req
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
                _req.post(
                    f"{client.base_url}/atm/logout",
                    headers={"x-session-token": client._session_token},
                    timeout=5,
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
        if not session.get("account"):
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
    if session.get("account"):
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
            account=session.get("account", ""),
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


@app.route("/login", methods=["POST"])
def login():
    account = (request.form.get("account") or "").strip()
    pin = (request.form.get("pin") or "").strip()

    if not account or not pin:
        return jsonify({"status": "error", "message": "Enter account number and PIN."}), 400

    try:
        accounts_repo = AccountsRepository(MIDDLEWARE_URL)
        auth_result = accounts_repo.authenticate_with_status(account, pin)
    except RuntimeError as e:
        return jsonify({"status": "error", "message": str(e)}), 503
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    auth_status = auth_result.get("status")

    if auth_status == "locked":
        remaining = int(auth_result.get("remaining_lock_seconds", 300))
        mins, secs = divmod(remaining, 60)
        return jsonify({
            "status": "locked",
            "message": f"Account locked. Try again in {mins:02d}:{secs:02d}.",
        }), 403

    if auth_status != "ok":
        attempts = auth_result.get("attempts_to_next_lock")
        msg = f"Invalid credentials. {attempts} attempt(s) left before lockout." if attempts else "Invalid account number or PIN."
        return jsonify({"status": "invalid", "message": msg}), 401

    # Success — create session
    import secrets as _s
    atm_key = _s.token_hex(8)
    transactions_repo = TransactionsRepository(accounts_repo)
    atm = ATMApp(accounts_repo, transactions_repo)
    atm.current_account = account
    _register_atm_session(atm_key, atm)

    user = auth_result["account"]
    session["account"] = account
    session["full_name"] = user.get("name", "Customer")
    session["user_id"] = user.get("account_id", account)
    session["atm_key"] = atm_key

    return jsonify({
        "status": "ok",
        "full_name": user.get("name", "Customer"),
        "balance": float(user.get("balance", 0)),
        "account": account,
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
            resp = _req.post(
                f"{MIDDLEWARE_URL}/atm/session/continue",
                headers={"x-session-token": token},
                timeout=5,
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
            "type": "WITHDRAW",
            "amount": amount,
            "blockchain_tx": result.get("blockchainTx"),
            "verify_url": result.get("verifyUrl"),
        })
        return jsonify({
            "status": "ok",
            "message": msg,
            "newBalance": result.get("newBalance", 0),
            "blockchainTx": result.get("blockchainTx", ""),
            "qr": qr,
        })

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
            "type": "DEPOSIT",
            "amount": amount,
            "blockchain_tx": result.get("blockchainTx"),
            "verify_url": result.get("verifyUrl"),
        })
        return jsonify({
            "status": "ok",
            "message": msg,
            "newBalance": result.get("newBalance", 0),
            "blockchainTx": result.get("blockchainTx", ""),
            "qr": qr,
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
        raw = atm.transactions_repo.get_transactions_for_account(session["account"], 20)
        recent = [
            {
                "type":          t.get("transactionType") or t.get("type", ""),
                "amount":        float(t.get("amount", 0) or 0),
                "timestamp":     str(t.get("createdAt") or t.get("created_at", "")),
                "status":        t.get("transactionStatus") or t.get("status", "APPROVED"),
                "transaction_id": t.get("transactionId"),
                "chain_status":  t.get("chainStatus", "PENDING_SUBMIT"),
                "blockchain_tx": t.get("blockchainTx", ""),
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
        resp = _req.get(
            f"{MIDDLEWARE_URL}/atm/tx-status/{transaction_id}",
            headers={"x-session-token": atm.accounts_repo.client._session_token},
            timeout=5,
        )
        return resp.json(), resp.status_code
    except Exception:
        return jsonify({"error": "unavailable"}), 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)