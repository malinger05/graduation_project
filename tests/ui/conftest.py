"""
Selenium UI tests — customer ATM + admin panel.

Starts Flask apps on random ports with HTTP backends mocked.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from .mock_admin import AdminMockState, install_admin_mocks
from .mock_middleware import MockState, install_middleware_mocks

_SCREENSHOT_DIR = Path(__file__).resolve().parents[2] / ".ui-test-screenshots"


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
def admin_mock_state() -> AdminMockState:
    s = AdminMockState()
    s.reset()
    return s


@pytest.fixture(scope="session")
def atm_base_url(mock_state: MockState):
    os.environ.setdefault("ATM_IDLE_PROMPT_SECONDS", "86400")
    os.environ.setdefault("ATM_PROMPT_TIMEOUT_SECONDS", "120")

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

    thread = threading.Thread(target=_run, daemon=True, name="ui-test-flask-atm")
    thread.start()

    base = f"http://127.0.0.1:{port}"
    _wait_for_http(f"{base}/atm")

    yield base

    for p in patchers:
        p.stop()


@pytest.fixture(scope="session")
def admin_base_url(admin_mock_state: AdminMockState):
    port = _free_port()
    patchers = install_admin_mocks(admin_mock_state)
    for p in patchers:
        p.start()

    admin_root = Path(__file__).resolve().parents[2] / "admin-app"
    import sys

    if str(admin_root) not in sys.path:
        sys.path.insert(0, str(admin_root))

    import admin_app as admin_module

    admin_module.app.config["TESTING"] = True
    admin_module.app.config["WTF_CSRF_ENABLED"] = True

    def _run():
        admin_module.app.run(
            host="127.0.0.1",
            port=port,
            threaded=True,
            use_reloader=False,
        )

    thread = threading.Thread(target=_run, daemon=True, name="ui-test-flask-admin")
    thread.start()

    base = f"http://127.0.0.1:{port}"
    _wait_for_http(f"{base}/login")

    yield base

    for p in patchers:
        p.stop()


@pytest.fixture
def driver(request):
    mobile = "mobile_viewport" in request.node.name
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    if mobile:
        opts.add_experimental_option(
            "mobileEmulation", {"deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3.0}}
        )
    else:
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

    if request.node.rep_call.failed if hasattr(request.node, "rep_call") else False:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _SCREENSHOT_DIR / f"{request.node.name}.png"
        try:
            browser.save_screenshot(str(path))
        except Exception:
            pass
    browser.quit()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def atm_url(atm_base_url: str) -> str:
    return f"{atm_base_url}/atm"


@pytest.fixture
def admin_url(admin_base_url: str) -> str:
    return admin_base_url


@pytest.fixture(autouse=True)
def reset_mock_state(mock_state: MockState, admin_mock_state: AdminMockState):
    mock_state.reset_all()
    admin_mock_state.reset()
    yield
