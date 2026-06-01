# UI tests (Selenium)

Browser tests for the **customer ATM** (`customer_app.py` + `templates/atm.html`) and **admin panel** (`admin-app/`).

Middleware and Core Banking are **mocked in-process** so you do not need the full stack running.

## Prerequisites

- Google Chrome installed
- Python deps: `pip install -r requirements.txt` (includes `selenium` and `webdriver-manager`)

## Run

```bash
cd /Users/silanurozkan/graduation_project
python -m pytest tests/ui -v -m ui
```

Skip UI tests in normal unit runs:

```bash
pytest -m "not ui"
```

Optional full-stack smoke (demo must be running):

```bash
export UI_E2E_ENABLED=1
export UI_E2E_BASE_URL=http://127.0.0.1:5001/atm
export UI_E2E_CARD=4111111111111111
export UI_E2E_PIN=your-pin
python -m pytest tests/ui/test_atm_e2e.py -v
```

Failed tests save screenshots under `.ui-test-screenshots/`.

## Test modules

| File | Coverage |
|------|----------|
| `test_atm_ui.py` | Idle, login, deposit, withdraw |
| `test_atm_lockout_ui.py` | Progressive lockout, timer, recovery |
| `test_atm_transactions_ui.py` | Insufficient funds, zero amount, keypad, history, QR modal |
| `test_atm_session_ui.py` | Logout, idle continue / sign out |
| `test_atm_security_ui.py` | Admin lock, PIN reset required + flow |
| `test_atm_card_setup_ui.py` | Card setup wizard |
| `test_atm_navigation_ui.py` | Balance ↔ menu, mobile viewport |
| `test_admin_ui.py` | Admin login, dashboard, pages, register form |
| `test_atm_e2e.py` | Full-stack smoke (`ui_e2e`, skipped by default) |

## Mocks

- `mock_middleware.py` — `MiddlewareClient` + `mw_http` for card setup
- `mock_admin.py` — admin `_cb` / `_cb_service` / `_mw` calls
