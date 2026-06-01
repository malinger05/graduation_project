"""Shared Selenium helpers for ATM UI tests."""
from __future__ import annotations

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Re-export for tests that import helpers only
__all__ = [
    "wait_page_active",
    "wait_login_error",
    "wait_login_account_step",
    "wait_login_pin_step",
    "click_idle_flow",
    "submit_login_step",
    "enter_account_number_only",
    "enter_account_and_continue",
    "submit_wrong_pin",
    "wait_locked_screen",
    "get_lock_countdown_text",
    "login_via_atm",
    "enter_amount_and_confirm",
]


def wait_page_active(driver, page_id: str, timeout: float = 15) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: "active"
        in (d.find_element(By.ID, f"page-{page_id}").get_attribute("class") or "")
    )


def wait_login_account_step(driver, timeout: float = 10) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.find_element(By.ID, "login-heading").text == "Enter account number"
    )


def wait_login_pin_step(driver, timeout: float = 10) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.find_element(By.ID, "login-heading").text == "Enter your PIN"
    )


def wait_login_error(driver, timeout: float = 10) -> str:
    err = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "login-error"))
    )
    WebDriverWait(driver, timeout).until(
        lambda d: "show" in (d.find_element(By.ID, "login-error").get_attribute("class") or "")
    )
    return err.text.strip()


def submit_login_step(driver) -> None:
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "#page-login .btn-yellow"))
    ).click()


def enter_account_number_only(driver, account_number: str) -> None:
    """Submit account/card on step 1 without waiting for the next screen."""
    wait_page_active(driver, "login")
    wait_login_account_step(driver)
    inp = driver.find_element(By.ID, "login-input")
    inp.clear()
    inp.send_keys(account_number)
    submit_login_step(driver)


def enter_account_and_continue(driver, account_number: str) -> None:
    enter_account_number_only(driver, account_number)
    WebDriverWait(driver, 15).until(
        lambda d: (
            d.find_element(By.ID, "login-heading").text == "Enter your PIN"
            or "active" in (d.find_element(By.ID, "page-locked").get_attribute("class") or "")
        )
    )
    if "active" in (driver.find_element(By.ID, "page-locked").get_attribute("class") or ""):
        return
    wait_login_pin_step(driver)


def submit_wrong_pin(driver, pin: str) -> str:
    """Submit a PIN on step 2; return the login error message (invalid) if shown."""
    wait_login_pin_step(driver)
    inp = driver.find_element(By.ID, "login-input")
    inp.clear()
    inp.send_keys(pin)
    submit_login_step(driver)
    return wait_login_error(driver)


def wait_locked_screen(driver, timeout: float = 15) -> None:
    wait_page_active(driver, "locked", timeout=timeout)
    WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "lock-countdown"))
    )


def get_lock_countdown_text(driver) -> str:
    return driver.find_element(By.ID, "lock-countdown").text.strip()


def click_idle_flow(driver, flow: str) -> None:
    driver.find_element(
        By.XPATH,
        f"//button[contains(@onclick, \"startFlow('{flow}')\")]",
    ).click()


def login_via_atm(driver, card_number: str, pin: str) -> None:
    """Complete the two-step login wizard on page-login."""
    enter_account_and_continue(driver, card_number)
    wait_login_pin_step(driver)
    inp = driver.find_element(By.ID, "login-input")
    inp.clear()
    inp.send_keys(pin)
    submit_login_step(driver)


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
