"""ATM deposit/withdraw edge cases and transaction history."""
from __future__ import annotations

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .helpers import (
    confirm_amount,
    enter_amount_via_keypad,
    login_to_menu,
    start_atm,
    wait_page_active,
)

pytestmark = pytest.mark.ui

CARD = "4111111111111111"
PIN = "1234"


class TestAtmWithdrawErrors:
    def test_insufficient_funds_shows_error(self, driver, atm_url, mock_state):
        mock_state.balance = 100.0
        login_to_menu(driver, atm_url, CARD, PIN)
        wait_page_active(driver, "menu")
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'menu-card') and contains(@onclick,\"doOp('withdraw')\")]",
        ).click()
        wait_page_active(driver, "amount")
        enter_amount_via_keypad(driver, "500")
        confirm_amount(driver)
        WebDriverWait(driver, 10).until(
            lambda d: "insufficient"
            in d.find_element(By.ID, "amount-hint").text.lower()
        )
        assert "has-error" in (
            driver.find_element(By.ID, "amount-display").get_attribute("class") or ""
        )


class TestAtmAmountValidation:
    def test_zero_amount_shows_hint(self, driver, atm_url):
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'menu-card') and contains(@onclick,\"doOp('deposit')\")]",
        ).click()
        wait_page_active(driver, "amount")
        confirm_amount(driver)
        WebDriverWait(driver, 10).until(
            lambda d: "has-error" in (d.find_element(By.ID, "amount-display").get_attribute("class") or "")
        )
        hint = driver.find_element(By.ID, "amount-hint").text.lower()
        assert "valid" in hint or "enter" in hint


class TestAtmKeypadAmount:
    def test_keypad_custom_amount_deposit(self, driver, atm_url, mock_state):
        login_to_menu(driver, atm_url, CARD, PIN)
        wait_page_active(driver, "menu")
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'menu-card') and contains(@onclick,\"doOp('deposit')\")]",
        ).click()
        wait_page_active(driver, "amount")
        enter_amount_via_keypad(driver, "125")
        confirm_amount(driver)
        WebDriverWait(driver, 25).until(
            EC.text_to_be_present_in_element((By.ID, "success-amount"), "+$125.00")
        )
        assert mock_state.balance == 625.0


class TestAtmTransactionHistory:
    def test_history_lists_transactions(self, driver, atm_url, mock_state):
        mock_state.transactions = mock_state.default_transactions()
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'menu-card') and contains(@onclick,\"showTransactions()\")]",
        ).click()
        wait_page_active(driver, "transactions")
        WebDriverWait(driver, 15).until(
            lambda d: "DEPOSIT" in d.find_element(By.ID, "txn-list").text.upper()
        )
        assert "WITHDRAW" in driver.find_element(By.ID, "txn-list").text.upper()


class TestAtmDepositQr:
    def test_deposit_with_blockchain_shows_qr_modal(self, driver, atm_url, mock_state):
        mock_state.deposit_include_blockchain = True
        login_to_menu(driver, atm_url, CARD, PIN)
        driver.find_element(
            By.XPATH,
            "//div[contains(@class,'menu-card') and contains(@onclick,\"doOp('deposit')\")]",
        ).click()
        wait_page_active(driver, "amount")
        driver.find_element(
            By.XPATH, "//button[contains(@onclick, 'setAmount(20,')]"
        ).click()
        confirm_amount(driver)
        WebDriverWait(driver, 25).until(
            EC.visibility_of_element_located((By.ID, "qr-modal-overlay"))
        )
        overlay = driver.find_element(By.ID, "qr-modal-overlay")
        assert "hidden" not in (overlay.get_attribute("class") or "")
