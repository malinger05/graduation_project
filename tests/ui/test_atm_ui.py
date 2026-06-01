"""
Selenium UI tests for the customer ATM single-page app (templates/atm.html).

Run: pytest tests/ui -v -m ui
Requires: Chrome + chromedriver (webdriver-manager installs driver if needed).
"""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .helpers import (
    click_idle_flow,
    enter_amount_and_confirm,
    login_via_atm,
    wait_login_error,
    wait_page_active,
)

pytestmark = pytest.mark.ui


class TestAtmIdleScreen:
    def test_idle_welcome_screen_loads(self, driver, atm_url):
        driver.get(atm_url)
        wait_page_active(driver, "idle")
        assert "Welcome" in driver.find_element(By.CSS_SELECTOR, ".idle-title").text
        assert driver.find_element(By.XPATH, "//div[@class='ab-label' and text()='Deposit']")


class TestAtmLogin:
    def test_invalid_pin_shows_error(self, driver, atm_url):
        driver.get(atm_url)
        click_idle_flow(driver, "balance")
        login_via_atm(driver, "4111111111111111", "0000")
        message = wait_login_error(driver)
        assert "Invalid" in message or "attempt" in message.lower()
        wait_page_active(driver, "login")

    def test_valid_login_reaches_balance_screen(self, driver, atm_url):
        driver.get(atm_url)
        click_idle_flow(driver, "balance")
        login_via_atm(driver, "4111111111111111", "1234")
        wait_page_active(driver, "balance", timeout=20)
        main = driver.find_element(By.ID, "bal-main").text.replace(",", "")
        assert "500" in main
        assert driver.find_element(By.ID, "bal-account").text.endswith("3000")


class TestAtmDeposit:
    def test_deposit_updates_balance(self, driver, atm_url, mock_state):
        driver.get(atm_url)
        click_idle_flow(driver, "deposit")
        login_via_atm(driver, "4111111111111111", "1234")

        # After login, deposit flow briefly shows menu then opens amount entry.
        WebDriverWait(driver, 20).until(
            lambda d: "active" in (d.find_element(By.ID, "page-amount").get_attribute("class") or "")
        )
        enter_amount_and_confirm(driver, 50)

        WebDriverWait(driver, 25).until(
            EC.text_to_be_present_in_element((By.ID, "success-amount"), "+$50.00")
        )
        wait_page_active(driver, "success")
        assert mock_state.balance == 550.0


class TestAtmWithdraw:
    def test_withdraw_returns_to_menu(self, driver, atm_url, mock_state):
        driver.get(atm_url)
        click_idle_flow(driver, "withdraw")
        login_via_atm(driver, "4111111111111111", "1234")

        WebDriverWait(driver, 15).until(
            lambda d: "active" in (d.find_element(By.ID, "page-amount").get_attribute("class") or "")
            or "active" in (d.find_element(By.ID, "page-menu").get_attribute("class") or "")
        )
        if "active" not in (driver.find_element(By.ID, "page-amount").get_attribute("class") or ""):
            driver.find_element(
                By.XPATH,
                "//div[contains(@class,'menu-card') and contains(@onclick,\"doOp('withdraw')\")]",
            ).click()
            wait_page_active(driver, "amount")

        enter_amount_and_confirm(driver, 20)
        # Cash dispense screen, then menu (4s animation in UI)
        wait_page_active(driver, "menu", timeout=25)
        assert mock_state.balance == 480.0
        assert "$480.00" in driver.find_element(By.ID, "menu-balance").text
