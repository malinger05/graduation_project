# Report fixes — align PDF with codebase

Apply these edits in your LaTeX/Word source (`Idempotency.pdf` was reviewed May 2026). Diagram PNGs: re-export from `docs/diagrams/*.puml` after pulls.

---

## 1. Session TTL (Table 9 and Table 10)

**Problem:** Table 9 says `1800 s (15 min)` — 1800 s is **30 minutes**, not 15. Table 10 says idle expiry **30 minutes** while `SESSION_TTL_SECONDS` default in code is **900 s (15 min)** (`config.py`, `sessions.py`).

**Fix Table 9 — middleware row:**

| Setting | Where | Value |
|---------|--------|--------|
| `SESSION_TTL_SECONDS` | Middleware (`session_state`) | **900 s (15 min)** default |

**Fix Table 10 — idle row:**

| Situation | Behaviour |
|-----------|-----------|
| Idle longer than **15 minutes** (no `get()` / `touch()`) | `get()` deletes row → **401**; `touch()` → **False** |

**Fix prose (§4.3 / §5.4):** Replace “15 minutes if no real activity” / “30 minutes” server cap with **15 minutes** for middleware `SESSION_TTL_SECONDS`, unless you configure a higher value in keychain.

**UI timers stay:** 120 s prompt + 60 s answer → logout (~3 min) is **stricter** than the 15 min server cap.

---

## 2. Database boundaries (§2.1)

**Problem:** `correlation_logs` and `routing_config` are described as fully in use; only **routing_config** is still planned.

**Replace middleware table list item 3 with:**

> **correlation_logs:** one `correlation_id` per business operation (e.g. deposit). Each step (idempotency, Core Banking call, blockchain submit) appends a row so engineers can trace the full path. Implemented in `correlation.py`.

**Replace item 4 with:**

> **routing_config:** *planned* — URLs and rate limits without code changes.

**Optional:** Note that `session_state.balance` is a **cache**, not a second ledger.

---

## 3. `correlation_id` in transaction_logs (Table 11)

**Problem:** Described as unused / nullable only for future work.

**Replace row text with:**

> Shared trace id for one operation (`correlation.new_correlation_id()` at start of login/deposit/withdraw/etc.). Copied into `transaction_logs` and into each row of `correlation_logs` for that operation. Search this id to see HTTP audit + step-by-step trace.

---

## 4. Transaction logs retention (Table 13) — keep, clarify wording

**Your Table 13 is correct** if it matches:

- `TRANSACTION_LOG_RETENTION_DAYS` default **90**
- `RETENTION_CLEANUP_INTERVAL_SECONDS` default **3600** (hourly)

**Clarify in §6.2:** Rows are **insert-only during normal operation**; the retention thread **deletes** audit rows older than the configured window (not immutable forever).

---

## 5. Castell citation (§6.1)

**Problem:** “play a greater role … than the user’s final balance” overstates the paper.

**Safer sentence:**

> Castell argues that disputed ATM withdrawals (“phantom withdrawals”) depend on reliable computer records, including transaction and audit logs, and that inference from the account balance alone is not enough to reconstruct what happened (Castell, 1996).

---

## 6. Transaction log flow text (§6.3)

**Fix typos / accuracy:**

| Current | Change to |
|---------|-----------|
| `log.event()` | `log_event()` |
| Step 7 “prepare data” only | Split: `_audit()` computes `duration_ms` and calls `log_event()` → `sanitize()` → `INSERT` |
| “prepare data before storing” | `_audit` bundles fields; `log_event` sanitizes and inserts |

**Failed-request path:** Errors use the **HTTP exception handler** → `log_event(outcome=error)` (not `_audit` in the handler body).

---

## 7. Figure / caption numbering (§5)

**Problem:** Text refers to “Figure 7” for deposit while list says “Figure 6: Phase 2 - Deposit”; login/logout captions swapped in places.

**Action:** Renumber figures sequentially in the source and make every `\ref{}` match the caption (suggested order: login → deposit → logout for session flow; keep 9–11 for transaction-log flows).

---

## 8. Optional new subsection — §4.4 `sessions.py` API

Ch. 4 ends at schema; API is only partly in Ch. 5. Add:

| Function | Role |
|----------|------|
| `create()` | After login: new `session_token`, store JWT + account context |
| `get()` | Validate token, refresh `last_active`, return context for banking calls |
| `touch()` | Refresh `last_active` only (`/atm/session/continue`) |
| `update_balance()` | After deposit/withdraw: cache `balanceAfter` |
| `remove()` | Logout: delete row |
| `cleanup_expired()` | Background: delete idle sessions |

Point to Figures 10–12 for `get()` / `touch()` logic.

---

## 9. `.env.example` (optional)

Add comment for middleware session TTL:

```env
# SESSION_TTL_SECONDS=900   # middleware session idle cap (15 min)
```

---

## Checklist

- [ ] Table 9: `900 s` / 15 min for `SESSION_TTL_SECONDS`
- [ ] Table 10: 15 min idle (not 30)
- [ ] §2.1: correlation_logs implemented; routing_config planned
- [ ] Table 11: `correlation_id` populated + links to `correlation_logs`
- [ ] §6.1: soften Castell wording
- [ ] §6.2–6.3: retention nuance, `log_event`, `_audit`
- [ ] Figure numbers consistent
- [ ] Re-export updated diagrams (02, 08–13)
