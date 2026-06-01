"""
Selenium UI tests — customer ATM (customer_app.py).

Starts Flask on a random localhost port with middleware HTTP calls mocked.
Requires Chrome and a matching chromedriver (webdriver-manager can install it).
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from .mock_middleware import MockState, install_middleware_mocks


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Server did not become ready: {url}")


@pytest.fixture(scope="session")
def mock_state() -> MockState:
    return MockState()


@pytest.fixture(scope="session")
def atm_base_url(mock_state: MockState):
    port = _free_port()
    patchers = install_middleware_mocks(mock_state)
    for p in patchers:
        p.start()

    import customer_app

    customer_app.app.config["TESTING"] = True
    customer_app.app.config["WTF_CSRF_ENABLED"] = True

    def _run():
        customer_app.app.run(
            host="127.0.0.1",
            port=port,
            threaded=True,
            use_reloader=False,
        )

    thread = threading.Thread(target=_run, daemon=True, name="ui-test-flask")
    thread.start()

    base = f"http://127.0.0.1:{port}"
    _wait_for_http(f"{base}/atm")

    yield base

    for p in patchers:
        p.stop()


@pytest.fixture
def driver(atm_base_url: str):
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")

    try:
        from webdriver_manager.chrome import ChromeDriverManager

        service = Service(ChromeDriverManager().install())
        browser = webdriver.Chrome(service=service, options=opts)
    except Exception:
        browser = webdriver.Chrome(options=opts)

    browser.implicitly_wait(2)
    browser.set_page_load_timeout(30)
    yield browser
    browser.quit()


@pytest.fixture
def atm_url(atm_base_url: str) -> str:
    return f"{atm_base_url}/atm"


@pytest.fixture(autouse=True)
def reset_mock_balance(mock_state: MockState):
    mock_state.balance = 500.0
    mock_state.next_transaction_id = 100
    mock_state.reset_lockouts()
    yield
