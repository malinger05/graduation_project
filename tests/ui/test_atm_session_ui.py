"""ATM logout and idle session prompt."""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .helpers import login_to_menu, wait_page_active

pytestmark = pytest.mark.ui

CARD = "4111111111111111"
PIN = "1234"


class TestAtmLogout:
    def test_logout_returns_to_idle(self, driver, atm_url):
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.execute_script("doLogout();")
        wait_page_active(driver, "idle", timeout=20)
        WebDriverWait(driver, 10).until(
            lambda d: "Welcome" in d.find_element(By.CSS_SELECTOR, ".idle-title").text
        )


class TestAtmIdlePrompt:
    def test_idle_continue_keeps_session(self, driver, atm_url):
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.execute_script("showIdlePrompt();")
        WebDriverWait(driver, 10).until(
            lambda d: "hidden"
            not in (d.find_element(By.ID, "idle-prompt-overlay").get_attribute("class") or "")
        )
        driver.find_element(By.ID, "idle-prompt-yes").click()
        WebDriverWait(driver, 10).until(
            lambda d: "hidden"
            in (d.find_element(By.ID, "idle-prompt-overlay").get_attribute("class") or "")
        )
        wait_page_active(driver, "menu")

    def test_idle_sign_out_returns_to_welcome(self, driver, atm_url):
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.execute_script("showIdlePrompt();")
        WebDriverWait(driver, 10).until(
            lambda d: "hidden"
            not in (d.find_element(By.ID, "idle-prompt-overlay").get_attribute("class") or "")
        )
        driver.find_element(By.ID, "idle-prompt-no").click()
        wait_page_active(driver, "idle", timeout=15)
