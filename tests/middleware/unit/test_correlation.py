"""Unit tests for correlation.py."""

from __future__ import annotations

import pytest

import correlation
import db
from models import CorrelationLog


class TestNewCorrelationId:
    def test_returns_hex_string(self):
        cid = correlation.new_correlation_id()
        assert isinstance(cid, str)
        assert len(cid) == 32

    def test_ids_are_unique(self):
        ids = {correlation.new_correlation_id() for _ in range(50)}
        assert len(ids) == 50


@pytest.mark.db
class TestLogStep:
    def test_log_step_persists_row(self, middleware_db):
        cid = correlation.new_correlation_id()
        correlation.log_step(
            cid, "request_received", "middleware", "ok",
            account_number="ACC1", endpoint="/atm/deposit",
            detail={"amount": 10},
        )
        with db.db_session() as s:
            rows = s.query(CorrelationLog).filter_by(correlation_id=cid).all()
        assert len(rows) == 1
        assert rows[0].step == "request_received"
        assert rows[0].status == "ok"

    def test_log_step_redacts_pin_in_detail(self, middleware_db):
        cid = correlation.new_correlation_id()
        correlation.log_step(
            cid, "login", "middleware", "ok",
            detail={"pin": "1234", "account": "A"},
        )
        with db.db_session() as s:
            row = s.query(CorrelationLog).filter_by(correlation_id=cid).one()
        assert row.detail["pin"] == "***REDACTED***"
        assert row.detail["account"] == "A"

    def test_empty_correlation_id_noop(self, middleware_db):
        correlation.log_step("", "step", "middleware", "ok")
        with db.db_session() as s:
            assert s.query(CorrelationLog).count() == 0

    def test_db_disabled_noop(self, db_disabled):
        correlation.log_step("abc", "step", "middleware", "ok")


@pytest.mark.db
class TestLogStepVariants:
    def test_log_step_with_message(self, middleware_db):
        cid = correlation.new_correlation_id()
        correlation.log_step(
            cid, "error", "core_banking", "error",
            message="timeout",
        )
        with db.db_session() as s:
            row = s.query(CorrelationLog).filter_by(correlation_id=cid).one()
        assert row.message == "timeout"

    def test_multiple_steps_same_correlation(self, middleware_db):
        cid = correlation.new_correlation_id()
        correlation.log_step(cid, "a", "middleware", "ok")
        correlation.log_step(cid, "b", "core_banking", "ok")
        with db.db_session() as s:
            assert s.query(CorrelationLog).filter_by(correlation_id=cid).count() == 2
