# Report diagrams (PlantUML)

Export PNGs for the thesis (from repo root):

```bash
plantuml docs/diagrams/*.puml
```

| File | Chapter | Description |
|------|---------|-------------|
| `18_login_lockouts_schema.puml` | 7 | `login_lockouts` table (incl. `must_reset_pin`) |
| `19_login_lockout_flow.puml` | 7 | Login paths: timed lock, permanent lock, PIN reset, invalid, success |
| `20_login_lockout_progressive.puml` | 7 | Progressive 15 → 30 min, permanent at 9 failures |
| `23_blockchain_inline_flow.puml` | 8 | Inline hash + Sepolia submit + PATCH after deposit/withdraw |
| `24_blockchain_chain_status.puml` | 8 | `chain_status` state machine (PENDING_SUBMIT → … → TAMPERED) |
| `25_blockchain_worker_overview.puml` | 9 | Three daemon threads + admin API + Sepolia |
| `26_worker_submit_retry.puml` | 9 | submit-retry job sequence |
| `27_worker_confirm_poll.puml` | 9 | confirm-poll job sequence |
| `28_worker_tamper_check.puml` | 9 | tamper-check job sequence |
| `29_middleware_testing_strategy.puml` | Testing | Middleware test types (unit → UAT) |
| `21_admin_unlock_and_pin_reset.puml` | 7 | Staff unlock → customer PIN reset → login |
| `22_pin_reset_after_unlock_state.puml` | 7 | State diagram: permanent lock through PIN reset |

Earlier idempotency / session / ACK diagrams may live in your thesis export folder; re-add `.puml` files here if needed.
