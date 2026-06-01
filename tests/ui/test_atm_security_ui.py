"""PIN reset required and admin-only lock screens."""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .helpers import (
    enter_account_number_only,
    start_atm,
    submit_login_step,
    wait_locked_screen,
    wait_login_pin_step,
)

pytestmark = pytest.mark.ui

CARD = "4111111111111111"
PIN = "1234"
NEW_PIN = "5678"


class TestAtmAdminLock:
    def test_admin_lock_screen_no_countdown(self, driver, atm_url, mock_state):
        mock_state.set_admin_locked(CARD)
        start_atm(driver, atm_url, "balance")
        enter_account_number_only(driver, CARD)
        wait_locked_screen(driver)
        assert "administrator" in driver.find_element(
            By.CSS_SELECTOR, "#page-locked .locked-sub"
        ).text.lower()
        assert driver.find_element(By.ID, "lock-countdown").text.strip() in ("—", "-", "")


class TestAtmPinResetRequired:
    def test_pin_reset_required_after_account_check(self, driver, atm_url, mock_state):
        mock_state.set_pin_reset_required(CARD)
        start_atm(driver, atm_url, "balance")
        enter_account_number_only(driver, CARD)
        WebDriverWait(driver, 15).until(
            lambda d: "active"
            in (d.find_element(By.ID, "page-pin-reset-required").get_attribute("class") or "")
        )

    def test_pin_reset_flow_then_login(self, driver, atm_url, mock_state):
        mock_state.set_pin_reset_required(CARD)
        start_atm(driver, atm_url, "balance")
        enter_account_number_only(driver, CARD)
        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(@onclick,'continuePinResetFromNotice')]")
            )
        ).click()
        WebDriverWait(driver, 15).until(
            lambda d: "choose new pin" in d.find_element(By.ID, "pin-reset-heading").text.lower()
        )
        inp = driver.find_element(By.ID, "pin-reset-input")
        inp.clear()
        inp.send_keys(NEW_PIN)
        driver.find_element(By.XPATH, "//button[contains(@onclick,'pinResetNext')]").click()
        WebDriverWait(driver, 15).until(
            lambda d: "confirm" in d.find_element(By.ID, "pin-reset-heading").text.lower()
        )
        inp.clear()
        inp.send_keys(NEW_PIN)
        driver.find_element(By.XPATH, "//button[contains(@onclick,'pinResetNext')]").click()
        WebDriverWait(driver, 20).until(
            lambda d: "active" in (d.find_element(By.ID, "page-pin-reset-success").get_attribute("class") or "")
        )
        driver.execute_script("pinResetSuccessDone();")
        start_atm(driver, atm_url, "balance")
        enter_account_number_only(driver, CARD)
        wait_login_pin_step(driver)
        inp = driver.find_element(By.ID, "login-input")
        inp.clear()
        inp.send_keys(NEW_PIN)
        submit_login_step(driver)
        WebDriverWait(driver, 20).until(
            lambda d: "active" in (d.find_element(By.ID, "page-balance").get_attribute("class") or "")
            or "active" in (d.find_element(By.ID, "page-menu").get_attribute("class") or "")
        )
