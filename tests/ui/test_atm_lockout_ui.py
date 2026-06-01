"""
Selenium UI tests for login lockout, countdown timer, and post-lock recovery.
"""
from __future__ import annotations

import re
import time

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .helpers import (
    click_idle_flow,
    enter_account_and_continue,
    enter_account_number_only,
    get_lock_countdown_text,
    submit_login_step,
    submit_wrong_pin,
    wait_locked_screen,
    wait_login_account_step,
    wait_login_error,
    wait_login_pin_step,
    wait_page_active,
)

pytestmark = pytest.mark.ui

WRONG_PIN = "0000"
CARD = "4111111111111111"


def _start_login(driver, atm_url) -> None:
    driver.get(atm_url)
    wait_page_active(driver, "idle")
    click_idle_flow(driver, "balance")


class TestAtmLockoutProgressive:
    def test_first_wrong_pin_shows_attempts_remaining(self, driver, atm_url):
        _start_login(driver, atm_url)
        enter_account_and_continue(driver, CARD)
        message = submit_wrong_pin(driver, WRONG_PIN)
        assert "2 attempt" in message.lower()
        wait_login_pin_step(driver)
        assert driver.find_element(By.ID, "login-input").get_attribute("value") == ""

    def test_second_wrong_pin_shows_one_attempt_left(self, driver, atm_url):
        _start_login(driver, atm_url)
        enter_account_and_continue(driver, CARD)
        submit_wrong_pin(driver, WRONG_PIN)
        message = submit_wrong_pin(driver, WRONG_PIN)
        assert "1 attempt" in message.lower()
        wait_login_pin_step(driver)

    def test_third_wrong_pin_shows_lock_screen_and_timer(self, driver, atm_url, mock_state):
        mock_state.lock_duration_seconds = 8
        _start_login(driver, atm_url)
        enter_account_and_continue(driver, CARD)
        for _ in range(2):
            submit_wrong_pin(driver, WRONG_PIN)
        inp = driver.find_element(By.ID, "login-input")
        inp.clear()
        inp.send_keys(WRONG_PIN)
        submit_login_step(driver)

        wait_locked_screen(driver)
        assert "Account Locked" in driver.find_element(By.CSS_SELECTOR, ".locked-title").text
        countdown = get_lock_countdown_text(driver)
        assert re.match(r"\d{2}:\d{2}", countdown), f"Expected MM:SS countdown, got {countdown!r}"
        assert countdown != "--:--"

    def test_lock_countdown_decreases(self, driver, atm_url, mock_state):
        mock_state.lock_duration_seconds = 8
        _start_login(driver, atm_url)
        enter_account_and_continue(driver, CARD)
        for _ in range(3):
            inp = driver.find_element(By.ID, "login-input")
            inp.clear()
            inp.send_keys(WRONG_PIN)
            submit_login_step(driver)
            if "active" in (driver.find_element(By.ID, "page-locked").get_attribute("class") or ""):
                break
            wait_login_error(driver)

        wait_locked_screen(driver)
        first = get_lock_countdown_text(driver)
        time.sleep(2)
        second = get_lock_countdown_text(driver)
        assert first != second


class TestAtmLockoutRecovery:
    def test_after_lock_expires_returns_to_idle(self, driver, atm_url, mock_state):
        mock_state.lock_duration_seconds = 3
        _start_login(driver, atm_url)
        enter_account_and_continue(driver, CARD)
        for _ in range(3):
            inp = driver.find_element(By.ID, "login-input")
            inp.clear()
            inp.send_keys(WRONG_PIN)
            submit_login_step(driver)
            if "active" in (driver.find_element(By.ID, "page-locked").get_attribute("class") or ""):
                break
            wait_login_error(driver)

        wait_locked_screen(driver)
        wait_page_active(driver, "idle", timeout=10)
        assert "Welcome" in driver.find_element(By.CSS_SELECTOR, ".idle-title").text

    def test_after_lock_new_login_starts_at_account_number(self, driver, atm_url, mock_state):
        mock_state.lock_duration_seconds = 3
        _start_login(driver, atm_url)
        enter_account_and_continue(driver, CARD)
        for _ in range(3):
            inp = driver.find_element(By.ID, "login-input")
            inp.clear()
            inp.send_keys(WRONG_PIN)
            submit_login_step(driver)
            if "active" in (driver.find_element(By.ID, "page-locked").get_attribute("class") or ""):
                break
            wait_login_error(driver)

        wait_locked_screen(driver)
        wait_page_active(driver, "idle", timeout=15)

        click_idle_flow(driver, "deposit")
        wait_page_active(driver, "login")
        wait_login_account_step(driver, timeout=15)
        assert driver.find_element(By.ID, "login-field-label").text.lower() == "account number"


class TestAtmLockoutPrecheck:
    def test_locked_account_on_card_entry_skips_pin(self, driver, atm_url, mock_state):
        mock_state.lock_card(CARD, seconds=60)
        _start_login(driver, atm_url)
        enter_account_number_only(driver, CARD)
        wait_locked_screen(driver)
        assert "Account Locked" in driver.find_element(By.CSS_SELECTOR, ".locked-title").text
        # PIN step should not be reachable while locked
        with pytest.raises(TimeoutException):
            WebDriverWait(driver, 3).until(
                lambda d: d.find_element(By.ID, "login-heading").text == "Enter your PIN"
            )
