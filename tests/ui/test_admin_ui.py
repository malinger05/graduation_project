"""Admin panel UI tests (mocked Core Banking + middleware)."""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

pytestmark = pytest.mark.ui

ADMIN_USER = "admin"
ADMIN_PASS = "admin123"


def _admin_login(driver, admin_url: str) -> None:
    driver.get(f"{admin_url}/login")
    driver.find_element(By.NAME, "username").send_keys(ADMIN_USER)
    driver.find_element(By.NAME, "password").send_keys(ADMIN_PASS)
    driver.find_element(By.CSS_SELECTOR, "button.login-btn").click()
    WebDriverWait(driver, 15).until(
        EC.url_contains("/dashboard")
    )


class TestAdminLogin:
    def test_invalid_credentials_show_error(self, driver, admin_url):
        driver.get(f"{admin_url}/login")
        driver.find_element(By.NAME, "username").send_keys("wrong")
        driver.find_element(By.NAME, "password").send_keys("wrong")
        driver.find_element(By.CSS_SELECTOR, "button.login-btn").click()
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".error-msg"))
        )
        assert "invalid" in driver.find_element(By.CSS_SELECTOR, ".error-msg").text.lower()

    def test_valid_login_shows_dashboard(self, driver, admin_url):
        _admin_login(driver, admin_url)
        assert driver.find_element(By.ID, "stat-total-customers").is_displayed()
        assert "Overview" in driver.page_source


class TestAdminNavigation:
    def test_transactions_page_loads(self, driver, admin_url):
        _admin_login(driver, admin_url)
        driver.get(f"{admin_url}/transactions")
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-admin-page='transactions']"))
        )

    def test_blockchain_page_loads(self, driver, admin_url):
        _admin_login(driver, admin_url)
        driver.get(f"{admin_url}/blockchain")
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-admin-page='blockchain']"))
        )

    def test_customers_page_loads(self, driver, admin_url):
        _admin_login(driver, admin_url)
        driver.get(f"{admin_url}/customers")
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "blocked-accounts-wrap"))
        )


class TestAdminRegister:
    def test_register_customer_form_loads(self, driver, admin_url):
        _admin_login(driver, admin_url)
        driver.get(f"{admin_url}/customers/register")
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "firstName"))
        )
