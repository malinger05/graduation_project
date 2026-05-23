# Middleware unit tests

Run from the project root (with `atm_venv` activated):

```bash
pip install -r requirements.txt
pytest tests/middleware/unit -v
```

With coverage:

```bash
pytest tests/middleware/unit \
  --cov=canonical --cov=lockouts --cov=sessions --cov=idempotency \
  --cov=blockchain_worker --cov=transaction_logs --cov=retention \
  --cov=correlation --cov=admin_client \
  --cov-report=term-missing
```

Add `atm-middleware` to the coverage path via:

```bash
pytest tests/middleware/unit --cov=atm-middleware --cov-report=term-missing
```

Markers:

- `@pytest.mark.memory` — in-memory lockouts/sessions (no Postgres)
- `@pytest.mark.db` — in-memory SQLite via the `middleware_db` fixture

Session tests pass `card_number` on `sessions.create()` (required since card-based login).
Lockout tests still use `account_number` (lockouts remain account-scoped per `models.py`).
