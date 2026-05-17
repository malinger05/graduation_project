# Architecture Diagrams (PlantUML)

Source files for the diagrams used in the middleware chapter of the report.

| File | Section | Purpose |
|---|---|---|
| `01_layered_architecture.puml` | §2 Overall Architecture | Where the middleware sits between the ATM, Core Banking, and Ethereum Sepolia. |
| `02_database_boundaries.puml` | §2.3 Two Databases | What `core_banking_db` owns vs. what `middleware_db` owns, and the no-shared-tables rule. |
| `03_deployment.puml` | §2.4 Deployment | Runtime view: FastAPI, Spring Boot, two Postgres containers, external Sepolia RPC. |
| `04_idempotency_flow.puml` | Idempotency | First request vs retry (deposit example). |
| `05_idempotency_begin_logic.puml` | Idempotency | Decision logic inside `begin()`. |
| `08_session_schema.puml` | Session management | `session_state` table columns and TTL rules. |
| `09_session_flow.puml` | Session management | Login → deposit → continue → logout (overview). |
| `10_session_get_logic.puml` | Session management | Decision logic inside `sessions.get()`. |
| `11_session_continue_flow.puml` | Session management | Phase 2b: idle prompt → `/atm/session/continue` → `touch()` (no Core Banking). |
| `12_session_touch_logic.puml` | Session management | Decision logic inside `sessions.touch()`. |

## Rendering

Pick whichever is easiest:

**VS Code / Cursor**
Install the *PlantUML* extension, open a `.puml` file, press `Alt+D` (or `Option+D`) for a live preview.

**CLI (requires Java + Graphviz)**
```bash
brew install plantuml          # macOS
plantuml docs/diagrams/*.puml  # writes .png next to each .puml
```

**Web (no install)**
Paste the file contents into <https://www.plantuml.com/plantuml/uml/>.

Export to PNG or SVG and drop the result into the report.
