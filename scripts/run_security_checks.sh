#!/usr/bin/env bash
# Run dependency vulnerability scan (pip-audit) and static security analysis (bandit).
# Usage: bash scripts/run_security_checks.sh
# Requires:
#   pip install -r requirements.txt
#   pip install -r requirements-dev.txt
# (Use any venv, e.g. source .venv/bin/activate or source atm_venv/bin/activate.)
#
# Exit code: 0 if both pass; 1 if either reports issues.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PA=0
BA=0

echo "== pip-audit (installed packages in the ACTIVE venv) =="
echo "    (Uses --local to avoid pip resolver failures in an isolated temp env.)"
set +e
pip-audit --local --desc on
PA=$?
set -e

if [ "$PA" -ne 0 ]; then
  echo ""
  echo "Note: To also try a dry-run install audit (may fail on complex pins):"
  echo "  pip-audit -r requirements.txt --desc on"
fi

# Only project code — never site-packages inside .venv (Bandit -x is unreliable for huge trees).
BANDIT_TARGETS=(
  atm_architecture.py
  atm.py
  customer_app.py
  secure_user_db.py
  secrets_manager.py
  worker.py
  view_db.py
  scripts
  tests
)

echo ""
echo "== bandit (severity HIGH only; project paths only) =="
set +e
bandit -r "${BANDIT_TARGETS[@]}" -lll -f txt
BA=$?
set -e

if [ "$BA" -ne 0 ]; then
  echo "(Bandit reported HIGH-severity issues.)"
fi

echo ""
echo "== bandit (MEDIUM+ for manual review; project paths only) =="
bandit -r "${BANDIT_TARGETS[@]}" -ll -f txt || true

echo ""
if [ "$PA" -ne 0 ] || [ "$BA" -ne 0 ]; then
  echo "Done with findings: pip-audit exit=$PA, bandit (HIGH-only) exit=$BA."
  echo "See README: pip-audit uses --local; bandit targets app + scripts + tests only."
  exit 1
fi
echo "Security checks finished with no reported issues."
exit 0
