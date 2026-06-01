# UI tests (Selenium)

Browser tests for the **customer ATM** (`customer_app.py` + `templates/atm.html`).

Middleware and Core Banking are **mocked in-process** so you do not need the full stack running.

## Prerequisites

- Google Chrome installed
- Python deps: `pip install -r requirements.txt` (includes `selenium` and `webdriver-manager`)

## Run

```bash
pytest tests/ui -v -m ui
```

Skip UI tests in normal CI/unit runs:

```bash
pytest -m "not ui"
```

## What is covered

| Test | Flow |
|------|------|
| Idle screen | `/atm` loads welcome + action buttons |
| Invalid PIN | Login error message |
| Login | Balance flow after successful auth |
| Deposit | Login → amount → success, balance +$50 |
| Withdraw | Login → amount → menu, balance −$20 |

## Full-stack UI (optional)

To hit real middleware instead of mocks, start `scripts/run_demo.sh` and set:

```bash
export UI_TEST_BASE_URL=https://atm.local
export UI_TEST_CARD=...
export UI_TEST_PIN=...
```

(Full-stack mode is not wired by default; extend `conftest.py` if you need it.)
