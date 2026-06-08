"""Unit tests for blockchain_dlq.py."""

from __future__ import annotations

import blockchain_dlq


def test_record_and_should_alert_first_time(middleware_db):
    assert blockchain_dlq.record_and_should_alert(
        42,
        account_number="ACC1",
        last_error="RPC timeout",
    )


def test_record_and_should_alert_no_duplicate(middleware_db):
    blockchain_dlq.record_and_should_alert(42, account_number="ACC1", last_error="err")
    blockchain_dlq.mark_notified(42)
    assert not blockchain_dlq.record_and_should_alert(
        42,
        account_number="ACC1",
        last_error="err",
    )


def test_record_and_should_alert_on_error_change(middleware_db):
    blockchain_dlq.record_and_should_alert(42, account_number="ACC1", last_error="a")
    blockchain_dlq.mark_notified(42)
    assert blockchain_dlq.record_and_should_alert(
        42,
        account_number="ACC1",
        last_error="b",
    )


def test_record_when_db_disabled(db_disabled):
    assert blockchain_dlq.record_and_should_alert(1, account_number=None, last_error="x")
