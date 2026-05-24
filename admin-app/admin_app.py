"""
admin_app.py — Admin Panel
Separate Flask app running on port 5002.
Talks directly to Core Banking (Spring Boot) with ROLE_ADMIN JWT.
Also uses X-Service-Token for /admin/transactions/* endpoints.

Responsibilities:
  - Admin login (gets JWT from /auth/login)
  - Register customers + accounts + set PIN
  - View all transactions with blockchain status
  - View all blockchain contracts (confirmed on-chain)
  - View all customers
"""

import os
import threading
import time
import sys
from functools import wraps
from flask import jsonify

import requests as _req
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from secrets_manager import get_secret

load_dotenv()

CORE_BANKING_URL = os.environ.get("CORE_BANKING_URL", "http://localhost:8080").rstrip("/")
MIDDLEWARE_URL   = os.environ.get("MIDDLEWARE_URL", "https://mw.local").rstrip("/")
SERVICE_TOKEN    = get_secret("MIDDLEWARE_SERVICE_TOKEN", "", allow_env_fallback=True).strip()

# Admin credentials stored in env — not in DB for simplicity
ADMIN_USERNAME = os.environ.get("ADMIN_PANEL_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PANEL_PASSWORD", "admin123")

app = Flask(__name__)
app.secret_key = get_secret("ADMIN_SECRET_KEY", "admin-change-me", allow_env_fallback=True)
app.config["SESSION_COOKIE_NAME"] = "admin_session"
app.config["SESSION_COOKIE_PATH"] = "/"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _jwt_headers():
    jwt = session.get("admin_jwt")
    if not jwt:
        return {}
    return {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}


def _service_headers():
    return {"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"}


def _cb(method, path, **kwargs):
    """Call Core Banking with JWT auth."""
    try:
        resp = getattr(_req, method)(
            f"{CORE_BANKING_URL}{path}",
            headers=_jwt_headers(),
            timeout=(3, 15),
            **kwargs
        )
        return resp
    except _req.exceptions.ConnectionError:
        return None


def _cb_service(method, path, **kwargs):
    """Call Core Banking with Service Token auth."""
    try:
        resp = getattr(_req, method)(
            f"{CORE_BANKING_URL}{path}",
            headers=_service_headers(),
            timeout=(3, 15),
            **kwargs
        )
        return resp
    except _req.exceptions.ConnectionError:
        return None


def _mw(method, path, **kwargs):
    """Call Middleware with Service Token auth."""
    try:
        resp = getattr(_req, method)(
            f"{MIDDLEWARE_URL}{path}",
            headers={"X-Service-Token": SERVICE_TOKEN, "Content-Type": "application/json"},
            timeout=(3, 10),
            **kwargs
        )
        return resp
    except _req.exceptions.ConnectionError:
        return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_jwt"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def _fetch_all_transactions(limit: int = 500) -> list:
    resp = _cb_service("get", "/admin/transactions", params={"limit": limit})
    if not resp or not resp.ok:
        return []
    return resp.json()


def _chain_status_counts(txns: list) -> dict:
    counts = {
        "pending": 0, "submitted": 0, "confirmed": 0,
        "failed": 0, "tampered": 0,
    }
    for t in txns:
        s = t.get("chainStatus") or "PENDING_SUBMIT"
        if s == "PENDING_SUBMIT":
            counts["pending"] += 1
        elif s == "SUBMITTED":
            counts["submitted"] += 1
        elif s == "CONFIRMED":
            counts["confirmed"] += 1
        elif s == "FAILED_SUBMIT":
            counts["failed"] += 1
        elif s == "TAMPERED":
            counts["tampered"] += 1
    return counts


def _fetch_dashboard_stats() -> dict:
    customers_resp = _cb("get", "/customers")
    customers = customers_resp.json() if customers_resp and customers_resp.ok else []
    txns = _fetch_all_transactions()
    counts = _chain_status_counts(txns)
    return {
        "totalCustomers": len(customers),
        "pendingCount": counts["pending"],
        "submittedCount": counts["submitted"],
        "confirmedCount": counts["confirmed"],
        "failedCount": counts["failed"],
        "tamperedCount": counts["tampered"],
    }


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("admin_jwt"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    # Check panel credentials first
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        flash("Invalid admin credentials.")
        return render_template("login.html")

    # Get JWT from Core Banking
    try:
        resp = _req.post(
            f"{CORE_BANKING_URL}/auth/login",
            json={"username": username, "password": password},
            timeout=(3, 10),
        )
    except _req.exceptions.ConnectionError:
        flash("Cannot reach Core Banking.")
        return render_template("login.html")

    if not resp.ok:
        flash("Core Banking login failed. Check ADMIN credentials match /auth/register.")
        return render_template("login.html")

    data = resp.json()
    session["admin_jwt"] = data.get("token")
    session["admin_username"] = username
    flash(f"Welcome, {username}.")
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.")
    return redirect(url_for("login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    stats = _fetch_dashboard_stats()
    return render_template("dashboard.html",
        admin=session.get("admin_username"),
        total_customers=stats["totalCustomers"],
        pending_count=stats["pendingCount"],
        submitted_count=stats["submittedCount"],
        confirmed_count=stats["confirmedCount"],
        failed_count=stats["failedCount"],
        tampered_count=stats["tamperedCount"],
    )


# ── Customers ─────────────────────────────────────────────────────────────────

@app.route("/customers")
@login_required
def customers():
    resp = _cb("get", "/customers")
    customer_list = resp.json() if resp and resp.ok else []
    return render_template("customers.html", customers=customer_list)


@app.route("/customers/register", methods=["GET", "POST"])
@login_required
def register_customer():
    if request.method == "GET":
        return render_template("register.html")

    f = request.form

    # Step 1: Create customer
    customer_payload = {
        "firstName":   f.get("firstName", "").strip(),
        "lastName":    f.get("lastName", "").strip(),
        "nationalId":  f.get("nationalId", "").strip(),
        "email":       f.get("email", "").strip(),
        "phoneNumber": f.get("phoneNumber", "").strip(),
        "dateOfBirth": f.get("dateOfBirth", "").strip(),
    }
    resp1 = _cb("post", "/customers", json=customer_payload)
    if not resp1 or not resp1.ok:
        detail = resp1.text[:200] if resp1 else "Core Banking unreachable"
        flash(f"Customer creation failed: {detail}")
        return render_template("register.html")

    customer = resp1.json()
    customer_id = customer["customerId"]

    # Step 2: Create account
    try:
        initial_balance = float(f.get("initialBalance", "0") or "0")
    except ValueError:
        initial_balance = 0.0

    resp2 = _cb("post", f"/customers/{customer_id}/accounts",
                json={"initialBalance": initial_balance})
    if not resp2 or not resp2.ok:
        detail = resp2.text[:200] if resp2 else "Core Banking unreachable"
        flash(f"Account creation failed: {detail} — customer created (id={customer_id})")
        return render_template("register.html")

    account = resp2.json()

    # Step 3: Issue a card
    resp_card = _cb("post", f"/accounts/{account['accountId']}/cards",
                    json={"holderName": f"{f.get('firstName','')} {f.get('lastName','')}".strip()})
    if not resp_card or not resp_card.ok:
        flash("Card issuance failed — customer and account created but no card/PIN set.")
        return render_template("register.html")

    card = resp_card.json()

    # Step 4: Set PIN on the card
    pin = f.get("pin", "").strip()
    if not pin or len(pin) != 4 or not pin.isdigit():
        flash("PIN must be exactly 4 digits — customer, account and card created but PIN not set.")
        return render_template("register.html")

    resp3 = _cb("post", "/atm/set-pin",
        json={"cardId": str(card["cardId"]), "pin": pin})
    if not resp3 or not resp3.ok:
        try:
            detail = resp3.json().get("error", resp3.text) if resp3 else "Core Banking unreachable"
        except Exception:
            detail = resp3.text if resp3 else "Core Banking unreachable"
        flash(f"PIN set failed: {detail} — customer, account and card created but PIN not set.")
        return render_template("register.html")

    flash(
        f"Customer registered successfully! "
        f"Account: {account['accountNumber']}. "
        f"Card: **** **** **** {card['cardNumber'][-4:]}. "
        f"PIN set."
    )
    return redirect(url_for("customers"))


# ── Transactions ──────────────────────────────────────────────────────────────

@app.route("/transactions")
@login_required
def transactions():
    filter_status = request.args.get("status", "ALL").upper()
    all_txns = _fetch_all_transactions()
    counts = _chain_status_counts(all_txns)
    if filter_status != "ALL":
        txn_list = [t for t in all_txns if (t.get("chainStatus") or "PENDING_SUBMIT") == filter_status]
    else:
        txn_list = all_txns
    txn_list.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
    return render_template("transactions.html",
        transactions=txn_list,
        filter_status=filter_status,
        pending_count=counts["pending"],
        submitted_count=counts["submitted"],
        confirmed_count=counts["confirmed"],
    )


# ── Blockchain ────────────────────────────────────────────────────────────────

@app.route("/blockchain")
@login_required
def blockchain():
    filter_type = request.args.get("type", "ALL").upper()
    all_txns = _fetch_all_transactions()
    confirmed = [t for t in all_txns if t.get("chainStatus") == "CONFIRMED"]
    tampered = [t for t in all_txns if t.get("chainStatus") == "TAMPERED"]
    if filter_type == "TAMPERED":
        contracts = tampered
    else:
        contracts = confirmed
    contracts.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
    return render_template("blockchain.html",
        contracts=contracts,
        filter_type=filter_type,
        confirmed_count=len(confirmed),
        tampered_count=len(tampered),
    )


# ── Live refresh JSON (admin UI polling) ──────────────────────────────────────

@app.route("/admin/api/dashboard")
@login_required
def api_dashboard():
    return jsonify(_fetch_dashboard_stats())


@app.route("/admin/api/transactions")
@login_required
def api_transactions():
    filter_status = request.args.get("status", "ALL").upper()
    all_txns = _fetch_all_transactions()
    counts = _chain_status_counts(all_txns)
    if filter_status != "ALL":
        txn_list = [t for t in all_txns if (t.get("chainStatus") or "PENDING_SUBMIT") == filter_status]
    else:
        txn_list = all_txns
    txn_list.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
    return jsonify({
        "filterStatus": filter_status,
        "counts": {
            "pending": counts["pending"],
            "submitted": counts["submitted"],
            "confirmed": counts["confirmed"],
            "failed": counts["failed"],
            "tampered": counts["tampered"],
        },
        "transactions": txn_list,
    })


@app.route("/admin/api/blockchain")
@login_required
def api_blockchain():
    filter_type = request.args.get("type", "ALL").upper()
    all_txns = _fetch_all_transactions()
    confirmed = [t for t in all_txns if t.get("chainStatus") == "CONFIRMED"]
    tampered = [t for t in all_txns if t.get("chainStatus") == "TAMPERED"]
    if filter_type == "TAMPERED":
        contracts = tampered
    else:
        contracts = confirmed
    contracts.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
    return jsonify({
        "filterType": filter_type,
        "confirmedCount": len(confirmed),
        "tamperedCount": len(tampered),
        "contracts": contracts,
    })


# ── Customer / Account APIs ───────────────────────────────────────────────────

@app.route("/admin/customer/<int:customer_id>/accounts")
@login_required
def customer_accounts(customer_id):
    """Returns accounts for a customer as JSON — called by the modal JS."""
    resp = _cb("get", f"/customers/{customer_id}/accounts")
    if not resp or not resp.ok:
        return jsonify([])
    return jsonify(resp.json())


@app.route("/admin/account/<account_number>/lockout-status")
@login_required
def account_lockout_status(account_number):
    """
    Returns lockout info for an account from the middleware.

    BUG FIX: The old code called /atm/account-status with {"accountNumber": ...}.
    The middleware's AccountStatusRequest model previously only accepted cardNumber
    and would resolve it via /atm/resolve-card. The admin panel has no card number —
    it works with account numbers.

    Fix: The middleware now accepts either cardNumber or accountNumber in
    AccountStatusRequest. We send accountNumber here so the middleware skips
    the card-resolution step and checks the lockout table directly.
    """
    resp = _mw("post", "/atm/account-status", json={"accountNumber": account_number})
    if not resp or not resp.ok:
        return jsonify({"status": "unknown", "error": "Middleware unreachable"}), 503
    return jsonify(resp.json())


@app.route("/admin/blocked-accounts")
@login_required
def blocked_accounts_list():
    """
    Returns JSON list of all permanently-locked accounts with their customer info.
    Fetches all customers + their accounts from Core Banking, then checks
    lockout status for each active account via middleware.

    BUG FIX: Same as account_lockout_status — now sends accountNumber instead of
    cardNumber to the middleware /atm/account-status endpoint.
    """
    customers_resp = _cb("get", "/customers")
    if not customers_resp or not customers_resp.ok:
        return jsonify({"error": "Cannot fetch customers"}), 503

    blocked = []
    for customer in customers_resp.json():
        cid = customer.get("customerId")
        if not cid:
            continue
        acc_resp = _cb("get", f"/customers/{cid}/accounts")
        if not acc_resp or not acc_resp.ok:
            continue
        for acc in acc_resp.json():
            account_number = acc.get("accountNumber")
            if not account_number or acc.get("accountStatus") == "CLOSED":
                continue
            # BUG FIX: send accountNumber, not cardNumber
            lock_resp = _mw("post", "/atm/account-status",
                            json={"accountNumber": account_number})
            if not lock_resp or not lock_resp.ok:
                continue
            try:
                lockout = lock_resp.json()
            except Exception:
                continue

            status = lockout.get("status")
            is_permanent = lockout.get("admin_unlock_required") is True
            is_pin_reset  = status == "pin_reset_required"

            if is_permanent or is_pin_reset:
                blocked.append({
                    "accountNumber":   account_number,
                    "accountId":       acc.get("accountId"),
                    "accountStatus":   acc.get("accountStatus"),
                    "balance":         acc.get("balance", 0),
                    "customerId":      cid,
                    "customerName":    f"{customer.get('firstName','')} {customer.get('lastName','')}".strip(),
                    "email":           customer.get("email", ""),
                    # BUG FIX: these three fields were missing — the unlock modal and
                    # blocked-accounts table need them to show the identity-verification
                    # panel so the admin can confirm the customer in person before unlocking.
                    "phone":           customer.get("phoneNumber", ""),
                    "nationalId":      customer.get("nationalId", ""),
                    "dateOfBirth":     customer.get("dateOfBirth", ""),
                    "lockStatus":      "pin_reset_required" if is_pin_reset else "permanently_locked",
                    "remainingSecs":   lockout.get("remaining_lock_seconds", 0),
                })

    return jsonify(blocked)


@app.route("/admin/account/<account_number>/unlock", methods=["POST"])
@login_required
def admin_unlock_account(account_number):
    """
    Staff unlock: calls middleware /atm/admin/login-unlock.
    The middleware sets must_reset_pin=True; the customer sets a new PIN at the ATM.
    """
    resp = _mw("post", "/atm/admin/login-unlock", json={"accountNumber": account_number})
    if not resp or not resp.ok:
        detail = resp.text[:200] if resp else "Middleware unreachable"
        return jsonify({"status": "error", "message": detail}), 502
    return jsonify({"status": "ok", "message": "Account unlocked. Customer must set a new PIN at the ATM."}), 200


# ── Card management ───────────────────────────────────────────────────────────

@app.route("/admin/account/<int:account_id>/cards")
@login_required
def account_cards(account_id):
    """Returns cards for an account as JSON."""
    resp = _cb("get", f"/accounts/{account_id}/cards")
    if not resp or not resp.ok:
        return jsonify([])
    return jsonify(resp.json())


@app.route("/admin/account/<int:account_id>/cards/issue", methods=["POST"])
@login_required
def issue_card(account_id):
    """Issue a new card for an account."""
    data = request.get_json(silent=True) or {}
    resp = _cb("post", f"/accounts/{account_id}/cards",
               json={"holderName": data.get("holderName", "")})
    if not resp or not resp.ok:
        detail = resp.text[:200] if resp else "Core Banking unreachable"
        return jsonify({"status": "error", "message": detail}), 502
    return jsonify(resp.json()), 201


@app.route("/admin/card/<int:card_id>/status", methods=["PATCH"])
@login_required
def update_card_status(card_id):
    """Block, unblock, or expire a card."""
    data = request.get_json(silent=True) or {}
    card_status = data.get("cardStatus")
    if not card_status:
        return jsonify({"status": "error", "message": "cardStatus required"}), 400
    # We need the accountId — fetch the card first via the admin transactions list
    # or require accountId in the request.
    account_id = data.get("accountId")
    if not account_id:
        return jsonify({"status": "error", "message": "accountId required"}), 400
    resp = _cb("patch", f"/accounts/{account_id}/cards/{card_id}/status",
               json={"cardStatus": card_status})
    if not resp or not resp.ok:
        detail = resp.text[:200] if resp else "Core Banking unreachable"
        return jsonify({"status": "error", "message": detail}), 502
    return jsonify(resp.json())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))
    app.run(host="0.0.0.0", port=port, debug=False)