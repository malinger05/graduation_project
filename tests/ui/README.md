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

| Test file | Flow |
|-----------|------|
| `test_atm_ui.py` | Idle screen, invalid PIN, login, deposit, withdraw |
| `test_atm_lockout_ui.py` | Progressive lockout (3 wrong PINs), countdown timer, return to idle, account step after lock, locked card pre-check |

### Lockout tests (mocked)

- 1st / 2nd wrong PIN → error on PIN step with attempts remaining (2, then 1)
- 3rd wrong PIN → **Account Locked** screen with `MM:SS` countdown
- Countdown ticks down every second
- When timer ends → **idle** welcome screen
- New login after lock → starts at **account number** step again
- Card already locked → locked screen before PIN entry

## Full-stack UI (optional)

To hit real middleware instead of mocks, start `scripts/run_demo.sh` and set:

```bash
export UI_TEST_BASE_URL=https://atm.local
export UI_TEST_CARD=...
export UI_TEST_PIN=...
```

(Full-stack mode is not wired by default; extend `conftest.py` if you need it.)
