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
SERVICE_TOKEN    = get_secret("MIDDLEWARE_SERVICE_TOKEN", "", allow_env_fallback=True).strip()

# Admin credentials stored in env — not in DB for simplicity
ADMIN_USERNAME = os.environ.get("ADMIN_PANEL_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PANEL_PASSWORD", "admin123")

app = Flask(__name__)
app.secret_key = get_secret("ADMIN_SECRET_KEY", "admin-change-me", allow_env_fallback=True)


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


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_jwt"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


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
        # Try to get JWT anyway using a known admin user
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
    # Quick stats
    customers_resp = _cb("get", "/customers")
    customers = customers_resp.json() if customers_resp and customers_resp.ok else []

    txns_resp = _cb_service("get", "/admin/transactions/pending-submit", params={"limit": 100})
    pending = txns_resp.json() if txns_resp and txns_resp.ok else []

    submitted_resp = _cb_service("get", "/admin/transactions/submitted", params={"limit": 100})
    submitted = submitted_resp.json() if submitted_resp and submitted_resp.ok else []

    confirmed_resp = _cb_service("get", "/admin/transactions/for-tamper-check", params={"limit": 100})
    confirmed = confirmed_resp.json() if confirmed_resp and confirmed_resp.ok else []

    return render_template("dashboard.html",
        admin=session.get("admin_username"),
        total_customers=len(customers),
        pending_count=len(pending),
        submitted_count=len(submitted),
        confirmed_count=len(confirmed),
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
    errors = []

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
        err = resp1.json() if resp1 else {"message": "Cannot reach Core Banking"}
        flash(f"Customer creation failed: {err.get('message', resp1.text if resp1 else 'connection error')}")
        return render_template("register.html", form=f)

    customer = resp1.json()
    customer_id = customer["customerId"]

    # Step 2: Create account
    initial_balance = f.get("initialBalance", "0").strip()
    try:
        initial_balance = float(initial_balance)
    except ValueError:
        initial_balance = 0.0

    resp2 = _cb("post", f"/customers/{customer_id}/accounts",
                json={"initialBalance": initial_balance})
    if not resp2 or not resp2.ok:
        err = resp2.json() if resp2 else {}
        flash(f"Account creation failed: {err.get('message', 'error')}. Customer #{customer_id} was created.")
        return render_template("register.html", form=f)

    account = resp2.json()

    # Step 3: Set PIN
    pin = f.get("pin", "").strip()
    if pin and len(pin) >= 4:
        resp3 = _cb("post", "/atm/set-pin",
                    json={"customerId": str(customer_id), "pin": pin})
        if not resp3 or not resp3.ok:
            flash(f"PIN set failed — customer #{customer_id} and account created but PIN not set.")
            return redirect(url_for("customers"))

    flash(f"✓ Customer {customer['firstName']} {customer['lastName']} registered. "
          f"Account: {account['accountNumber']}. "
          f"Customer ID: {customer_id}.")
    return redirect(url_for("customers"))


# ── Transactions ──────────────────────────────────────────────────────────────

@app.route("/transactions")
@login_required
def transactions():
    # Fetch all transaction categories
    pending_resp   = _cb_service("get", "/admin/transactions/pending-submit", params={"limit": 100, "maxAttempts": 99})
    submitted_resp = _cb_service("get", "/admin/transactions/submitted",      params={"limit": 100})
    confirmed_resp = _cb_service("get", "/admin/transactions/for-tamper-check", params={"limit": 200})

    pending   = pending_resp.json()   if pending_resp   and pending_resp.ok   else []
    submitted = submitted_resp.json() if submitted_resp and submitted_resp.ok else []
    confirmed = confirmed_resp.json() if confirmed_resp and confirmed_resp.ok else []

    # Combine and deduplicate by transactionId, keeping latest status
    seen = {}
    for t in confirmed:
        seen[t["transactionId"]] = t
    for t in submitted:
        seen[t["transactionId"]] = t
    for t in pending:
        seen[t["transactionId"]] = t

    all_txns = sorted(seen.values(), key=lambda x: x.get("createdAt", ""), reverse=True)

    filter_status = request.args.get("status", "ALL")
    if filter_status != "ALL":
        all_txns = [t for t in all_txns if t.get("chainStatus") == filter_status]

    return render_template("transactions.html",
        transactions=all_txns,
        filter_status=filter_status,
        pending_count=len(pending),
        submitted_count=len(submitted),
        confirmed_count=len(confirmed),
    )


# ── Blockchain contracts ──────────────────────────────────────────────────────

@app.route("/blockchain")
@login_required
def blockchain():
    resp = _cb_service("get", "/admin/transactions/for-tamper-check", params={"limit": 200})
    contracts = resp.json() if resp and resp.ok else []

    # Sort by created desc
    contracts = sorted(contracts, key=lambda x: x.get("createdAt", ""), reverse=True)

    filter_type = request.args.get("type", "ALL")
    if filter_type == "TAMPERED":
        contracts = [c for c in contracts if c.get("chainStatus") == "TAMPERED"]
    elif filter_type == "CONFIRMED":
        contracts = [c for c in contracts if c.get("chainStatus") == "CONFIRMED"]

    tampered_count  = sum(1 for c in contracts if c.get("chainStatus") == "TAMPERED")
    confirmed_count = sum(1 for c in contracts if c.get("chainStatus") == "CONFIRMED")

    return render_template("blockchain.html",
        contracts=contracts,
        filter_type=filter_type,
        tampered_count=tampered_count,
        confirmed_count=confirmed_count,
    )

 
@app.route("/admin/customer/<int:customer_id>/accounts")
@login_required
def customer_accounts(customer_id):
    """Returns accounts for a customer as JSON — called by the modal JS."""
    resp = _cb("get", f"/customers/{customer_id}/accounts")
    if not resp or not resp.ok:
        return jsonify([]), 200
    return jsonify(resp.json()), 200
 

if __name__ == "__main__":
    port = int(os.environ.get("ADMIN_PORT", "5002"))
    print(f"[Admin Panel] Running on http://0.0.0.0:{port}")
    print(f"[Admin Panel] Core Banking: {CORE_BANKING_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)