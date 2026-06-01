"""Shared Selenium helpers for ATM UI tests."""
from __future__ import annotations

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Re-export for tests that import helpers only
__all__ = [
    "wait_page_active",
    "wait_login_error",
    "click_idle_flow",
    "login_via_atm",
    "enter_amount_and_confirm",
]


def wait_page_active(driver, page_id: str, timeout: float = 15) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: "active"
        in (d.find_element(By.ID, f"page-{page_id}").get_attribute("class") or "")
    )


def wait_login_error(driver, timeout: float = 10) -> str:
    err = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "login-error"))
    )
    WebDriverWait(driver, timeout).until(
        lambda d: "show" in (d.find_element(By.ID, "login-error").get_attribute("class") or "")
    )
    return err.text.strip()


def click_idle_flow(driver, flow: str) -> None:
    driver.find_element(
        By.XPATH,
        f"//button[contains(@onclick, \"startFlow('{flow}')\")]",
    ).click()


def login_via_atm(driver, card_number: str, pin: str) -> None:
    """Complete the two-step login wizard on page-login."""
    wait_page_active(driver, "login")
    inp = driver.find_element(By.ID, "login-input")
    inp.clear()
    inp.send_keys(card_number)
    driver.find_element(By.CSS_SELECTOR, "#page-login .btn-yellow").click()

    WebDriverWait(driver, 10).until(
        lambda d: d.find_element(By.ID, "login-heading").text == "Enter your PIN"
    )

    inp = driver.find_element(By.ID, "login-input")
    inp.clear()
    inp.send_keys(pin)
    driver.find_element(By.CSS_SELECTOR, "#page-login .btn-yellow").click()


def enter_amount_and_confirm(driver, amount: int) -> None:
    wait_page_active(driver, "amount")
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable(
            (By.XPATH, f"//button[contains(@onclick, 'setAmount({amount},')]")
        )
    ).click()
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, ".confirm-key"))
    ).click()
