"""
Shared HTTP security headers for Flask Layer 1 apps (customer + admin).

Addresses OWASP ZAP passive findings: CSP, clickjacking, MIME sniffing.
"""
from __future__ import annotations

import os

# atm.html / admin templates use inline <style> and <script>; nonces would be a follow-up.
_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "form-action 'self'"
)


def configure_session_cookies(app) -> None:
    """Harden Flask session cookies (HttpOnly is Flask default)."""
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    secure = os.environ.get("FLASK_SESSION_SECURE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    app.config["SESSION_COOKIE_SECURE"] = secure


def apply_security_headers(response):
    """Attach headers expected by ZAP / OWASP security header checks."""
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=()"
    )
    # Werkzeug dev server may still emit Server; production proxy should strip it.
    response.headers.pop("Server", None)
    return response


def register_security_headers(app) -> None:
    """Call once after Flask app creation."""

    @app.after_request
    def _security_headers(response):
        return apply_security_headers(response)


def suppress_werkzeug_server_version() -> None:
    """Hide Python/Werkzeug versions in the Server header (dev server only)."""
    try:
        from werkzeug.serving import WSGIRequestHandler

        WSGIRequestHandler.server_version = ""
    except Exception:
        pass
