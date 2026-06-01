"""
Full-stack UI smoke (optional).

Requires the demo stack running and env vars:

  export UI_E2E_ENABLED=1
  export UI_E2E_BASE_URL=http://127.0.0.1:5001/atm   # or https://atm.local
  export UI_E2E_CARD=...
  export UI_E2E_PIN=...
"""
from __future__ import annotations

import os

import pytest
from selenium.webdriver.common.by import By

from .helpers import login_via_atm, start_atm, wait_page_active

pytestmark = [pytest.mark.ui, pytest.mark.ui_e2e]

_skip = not os.environ.get("UI_E2E_ENABLED", "").strip() in ("1", "true", "yes")
_e2e_url = os.environ.get("UI_E2E_BASE_URL", "http://127.0.0.1:5001/atm").rstrip("/")
_e2e_card = os.environ.get("UI_E2E_CARD", "")
_e2e_pin = os.environ.get("UI_E2E_PIN", "")


@pytest.fixture
def e2e_atm_url():
    if _skip:
        pytest.skip("Set UI_E2E_ENABLED=1 to run full-stack UI tests")
    return _e2e_url if _e2e_url.endswith("/atm") else f"{_e2e_url}/atm"


@pytest.mark.skipif(_skip or not _e2e_card or not _e2e_pin, reason="UI_E2E_CARD/PIN required")
class TestAtmFullStackSmoke:
    def test_login_reaches_menu_or_balance(self, driver, e2e_atm_url):
        start_atm(driver, e2e_atm_url, "balance")
        login_via_atm(driver, _e2e_card, _e2e_pin)
        assert (
            "active"
            in (
                driver.find_element(By.ID, "page-menu").get_attribute("class") or ""
            )
            or "active"
            in (
                driver.find_element(By.ID, "page-balance").get_attribute("class") or ""
            )
        )
