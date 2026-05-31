"""
Flask-WTF CSRF for Layer 1 apps (customer ATM UI + admin panel).
"""
from __future__ import annotations

from flask import jsonify, request
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf

csrf = CSRFProtect()


def _wants_json_response() -> bool:
    if request.is_json:
        return True
    if request.headers.get("X-CSRFToken"):
        return True
    accept = request.headers.get("Accept") or ""
    return "application/json" in accept


def register_csrf(app) -> None:
    app.config.setdefault("WTF_CSRF_SSL_STRICT", False)
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)
    csrf.init_app(app)

    @app.context_processor
    def inject_csrf():
        return dict(csrf_token=generate_csrf)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(_exc):
        if _wants_json_response():
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "CSRF token missing or invalid. Refresh the page and try again.",
                    }
                ),
                400,
            )
        return "CSRF validation failed. Refresh the page and try again.", 400
