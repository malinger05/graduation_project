"""Unit tests for config.py parsing helpers (no module reload)."""

from __future__ import annotations

from unittest.mock import patch

import config


class TestConfigParsers:
    def test_str_strips_value(self):
        with patch("config.get_secret", return_value="  hello  "):
            assert config._str("ANY") == "hello"

    def test_int_parser(self):
        with patch("config._str", return_value="42"):
            assert config._int("X", 0) == 42

    def test_int_default_on_empty(self):
        with patch("config._str", return_value=""):
            assert config._int("X", 99) == 99

    def test_float_parser(self):
        with patch("config._str", return_value="3.5"):
            assert config._float("X", 0.0) == 3.5

    def test_lockout_minutes_fast_test(self):
        with patch("config._str", side_effect=lambda n, d="": "1" if n == "LOCKOUT_FAST_TEST" else d):
            mins = config._lockout_minutes_list()
            assert mins == [0.1, 0.15]

    def test_lockout_minutes_custom_csv(self):
        with patch("config._str", side_effect=lambda n, d="": "5,10" if n == "LOCKOUT_MINUTES" else d):
            assert config._lockout_minutes_list() == [5.0, 10.0]
