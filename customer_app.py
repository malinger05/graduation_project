"""
customer_app.py  —  Layer 1
Flask web UI. Talks ONLY to middleware (Layer 2).
No PostgreSQL. No blockchain. No Spring Boot calls.

[FIX] Per-session ATMApp instances now carry an expiry timestamp.
      A background cleanup thread evicts sessions idle for longer than
      ATM_SESSION_TTL_SECONDS (default: 30 minutes).
      Logout removes the session immediately.
"""
import os
import io
import base64
import threading
import time
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

# ── Per-session ATM instances ─────────────────────────────────────────────────
#
# CRITICAL FIX: The original _atm_sessions dict grew without bound — sessions
# were only removed on explicit logout, so a browser that was closed without
# logging out (or a network error mid-logout) left a dead entry forever.
# Under sustained load this would exhaust server memory.
#
# Fix:
#   - Each entry stores a last_active timestamp alongside the ATMApp instance.
#   - _session_cleanup() runs in a daemon thread every 60s and evicts entries
#     idle for longer than ATM_SESSION_TTL_SECONDS.
#   - _touch_session() updates last_active on every authenticated request.
#   - logout() and the eviction loop both call _evict_atm_session() which
#     also sends a logout signal to the middleware so its session is cleaned up.
# ─────────────────────────────────────────────────────────────────────────────

# How long (seconds) an ATM session may be idle before eviction (default 30 min)
ATM_SESSION_TTL_SECONDS = int(os.environ.get("ATM_SESSION_TTL_SECONDS", "1800"))

# Maps atm_key → {"atm": ATMApp, "last_active": float}
_atm_sessions: dict[str, dict] = {}
_atm_sessions_lock = threading.Lock()


def _evict_atm_session(atm_key: str) -> None:
    """Remove one session entry and signal middleware to drop its session."""
    with _atm_sessions_lock:
        entry = _atm_sessions.pop(atm_key, None)
    if entry:
        try:
            # Best-effort: tell middleware to invalidate its session token too
            atm: ATMApp = entry["atm"]
            client = atm.accounts_repo.client
            if client._session_token:
                import requests as _req
                _req.post(
                    f"{client.base_url}/atm/logout",
                    headers={"x-session-token": client._session_token},
                    timeout=5,
                )
        except Exception:
            pass  # Middleware logout is best-effort; local eviction always completes


def _session_cleanup() -> None:
    """Daemon thread: evict idle ATM sessions every 60 seconds."""
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


# Start the cleanup daemon at import time (before first request)
_cleanup_thread = threading.Thread(target=_session_cleanup, daemon=True)
_cleanup_thread.start()


def _get_session_atm() -> ATMApp | None:
    """Return the ATMApp for the current Flask session, updating last_active."""
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


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Routes ────────────────────────────────────────────────────────────────────

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
    _register_atm_session(atm_key, atm)

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
        _evict_atm_session(key)
    session.clear()
    flash("You are signed out.")
    return redirect(url_for("login"))


@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    atm = _get_session_atm()
    if not atm:
        session.clear()
        return redirect(url_for("login"))

    try:
        balance = atm.check_balance()
        raw_txns = atm.transactions_repo.get_transactions_for_account(session["account"], 10)
        recent = [
            {
                # Core Banking returns camelCase — transactionType, transactionStatus, createdAt
                "type":      t.get("transactionType") or t.get("type", ""),
                "amount":    float(t.get("amount", 0) or 0),
                "timestamp": str(t.get("createdAt") or t.get("created_at", "")),
                "status":    t.get("transactionStatus") or t.get("status", "APPROVED"),
            }
            for t in raw_txns
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

    atm = _get_session_atm()
    if not atm:
        return redirect(url_for("login"))

    try:
        ok, msg, result = atm.accounts_repo.client.withdraw(amount)
        if ok and isinstance(result, dict):
            _attach_qr({
                "type":          "WITHDRAW",
                "amount":        amount,
                "blockchain_tx": result.get("blockchainTx"),
            })
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

    atm = _get_session_atm()
    if not atm:
        return redirect(url_for("login"))

    try:
        ok, msg, result = atm.accounts_repo.client.deposit(amount)
        if ok and isinstance(result, dict):
            _attach_qr({
                "type":          "DEPOSIT",
                "amount":        amount,
                "blockchain_tx": result.get("blockchainTx"),
            })
    except Exception as e:
        return render_template("config_error.html", error=str(e)), 503

    flash(msg)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)