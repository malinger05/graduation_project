"""Self-service card setup wizard in the ATM SPA."""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .helpers import wait_login_account_step, wait_page_active

pytestmark = pytest.mark.ui

ACCOUNT = "DE89370400440532013000"
PIN = "4321"


class TestAtmCardSetup:
    def test_card_setup_happy_path(self, driver, atm_url):
        driver.get(atm_url)
        wait_page_active(driver, "idle")
        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(@onclick,'goToCardSetup')]")
            )
        ).click()
        wait_page_active(driver, "card-setup")

        acct = driver.find_element(By.ID, "cs-input-account")
        acct.clear()
        acct.send_keys(ACCOUNT)
        driver.find_element(By.XPATH, "//button[contains(@onclick,'csNext')]").click()

        WebDriverWait(driver, 20).until(
            lambda d: d.find_element(By.ID, "cs-panel-pin").is_displayed()
        )
        driver.find_element(By.ID, "cs-input-account-confirm").clear()
        driver.find_element(By.ID, "cs-input-account-confirm").send_keys(ACCOUNT)
        driver.find_element(By.ID, "cs-input-pin").send_keys(PIN)
        driver.find_element(By.ID, "cs-input-pin2").send_keys(PIN)
        driver.find_element(By.ID, "cs-pin-btn").click()

        WebDriverWait(driver, 20).until(
            lambda d: d.find_element(By.ID, "cs-panel-done").is_displayed()
        )
        assert "ready" in driver.find_element(By.ID, "cs-done-card").text.lower()

        driver.find_element(By.XPATH, "//button[contains(.,'Go to login')]").click()
        wait_page_active(driver, "idle")
        driver.find_element(By.XPATH, "//button[contains(@onclick,\"startFlow('balance')\")]").click()
        wait_login_account_step(driver)
