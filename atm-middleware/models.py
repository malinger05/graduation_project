"""
models.py  —  SQLAlchemy table definitions for the middleware's own database.

Tables:
  - IdempotencyRecord  (idempotency_records)
  - SessionState       (session_state)
  - TransactionLog     (transaction_logs)
  - CorrelationLog     (correlation_logs)

Still planned: routing_config.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from db import Base


class IdempotencyRecord(Base):
    """
    "Have I seen this request before?"

    Stores every (account_number, idempotency_key) pair the middleware has
    handled, along with the response it produced. Before processing a
    state-mutating request (deposit/withdraw), the middleware looks here
    first: if the same key already has a `completed` row, the cached
    response is returned and Core Banking is NOT called again.

    Prevents: duplicate deposits/withdrawals caused by network retries.
    """

    __tablename__ = "idempotency_records"

    # Composite primary key — keys are scoped per account so two different
    # users sending the same client-generated key cannot collide.
    account_number       = Column(String, primary_key=True)
    idempotency_key      = Column(String, primary_key=True)

    endpoint             = Column(String, nullable=False)

    # SHA-256 of (endpoint, account_number, request_body). If a client reuses
    # the same key with a different body we return 422 — that's almost always
    # a client bug and silently aliasing it would be dangerous.
    request_fingerprint  = Column(String, nullable=False)

    # 'in_progress'  — request is currently being forwarded to Core Banking.
    # 'completed'    — response is cached in response_body and safe to replay.
    status               = Column(String, nullable=False)

    response_status_code = Column(Integer, nullable=True)
    response_body        = Column(JSONB,   nullable=True)

    created_at           = Column(DateTime(timezone=True), nullable=False)
    completed_at         = Column(DateTime(timezone=True), nullable=True)

    # After this timestamp the key may be reused for a new request. 24h is the
    # industry default (Stripe, AWS).
    expires_at           = Column(DateTime(timezone=True), nullable=False)


class SessionState(Base):
    """
    "Who is this person and are they still logged in?"

    Maps an opaque session_token (sent as X-Session-Token) to the Core Banking
    JWT and cached account context so every ATM action does not re-authenticate.
    """

    __tablename__ = "session_state"

    session_token  = Column(String, primary_key=True)

    jwt            = Column(String, nullable=False)
    account_id     = Column(Integer, nullable=False)
    account_number = Column(String, nullable=False)
    balance        = Column(Float, nullable=False)
    customer_name  = Column(String, nullable=False)

    last_active    = Column(DateTime(timezone=True), nullable=False)
    created_at     = Column(DateTime(timezone=True), nullable=False)


class TransactionLog(Base):
    """
    Append-only audit log of requests handled by the middleware.

    Records who called which endpoint, with what outcome — for regulators,
    disputes, and operational review. Does not replace Core Banking's ledger.
    """

    __tablename__ = "transaction_logs"

    log_id               = Column(String, primary_key=True)
    correlation_id       = Column(String, nullable=True)

    created_at           = Column(DateTime(timezone=True), nullable=False)
    account_number       = Column(String, nullable=True)
    channel              = Column(String, nullable=False)
    http_method          = Column(String, nullable=False)
    endpoint             = Column(String, nullable=False)
    idempotency_key      = Column(String, nullable=True)

    request_body         = Column(JSONB, nullable=True)
    response_status_code = Column(Integer, nullable=False)
    response_body        = Column(JSONB, nullable=True)

    # success | error | cached (idempotency replay — no Core Banking call)
    outcome              = Column(String, nullable=False)
    duration_ms          = Column(Integer, nullable=True)
    error_message        = Column(String, nullable=True)


class CorrelationLog(Base):
    """
    Trace one business operation (deposit, withdraw, login) across systems.

    Many rows share the same correlation_id — one row per step (idempotency,
    Core Banking call, blockchain submit, etc.).
    """

    __tablename__ = "correlation_logs"

    log_id         = Column(String, primary_key=True)
    correlation_id = Column(String, nullable=False, index=True)

    step           = Column(String, nullable=False)
    system         = Column(String, nullable=False)
    status         = Column(String, nullable=False)
    message        = Column(String, nullable=True)
    detail         = Column(JSONB, nullable=True)

    account_number = Column(String, nullable=True)
    endpoint       = Column(String, nullable=True)
    created_at     = Column(DateTime(timezone=True), nullable=False)
