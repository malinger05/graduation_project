"""Menu navigation and viewport smoke tests."""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .helpers import login_to_menu, start_atm, wait_page_active

pytestmark = pytest.mark.ui

CARD = "4111111111111111"
PIN = "1234"


class TestAtmNavigation:
    def test_menu_balance_and_back(self, driver, atm_url):
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'menu-card') and contains(@onclick,'showBalancePage')]",
        ).click()
        wait_page_active(driver, "balance")
        assert "500" in driver.find_element(By.ID, "bal-main").text.replace(",", "")
        driver.find_element(By.CSS_SELECTOR, "#page-balance .back-btn").click()
        wait_page_active(driver, "menu")

    def test_balance_footer_returns_to_menu(self, driver, atm_url):
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'menu-card') and contains(@onclick,'showBalancePage')]",
        ).click()
        wait_page_active(driver, "balance")
        driver.find_element(
            By.XPATH, "//button[contains(@onclick,\"goTo('menu')\") and contains(.,'Menu')]"
        ).click()
        wait_page_active(driver, "menu")


class TestAtmMobileViewport:
    def test_idle_screen_loads_mobile_viewport(self, driver, atm_url):
        driver.get(atm_url)
        wait_page_active(driver, "idle")
        assert driver.execute_script("return window.innerWidth") <= 420
        assert "Welcome" in driver.find_element(By.CSS_SELECTOR, ".idle-title").text
