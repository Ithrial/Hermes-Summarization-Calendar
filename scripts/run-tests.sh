#!/usr/bin/env bash
# Run the complete Daily Ledger release gate without external model calls.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

if [ -n "${PYTHON_BIN:-}" ]; then
    PYTHON="$PYTHON_BIN"
elif [ -x "$HERMES_HOME/hermes-agent/venv/bin/python" ]; then
    PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python"
else
    PYTHON="python3"
fi

command -v "$PYTHON" >/dev/null 2>&1 || {
    echo "ERROR: Python interpreter not found: $PYTHON" >&2
    exit 1
}
command -v node >/dev/null 2>&1 || {
    echo "ERROR: Node.js is required for frontend tests" >&2
    exit 1
}

GUARD_LOG="$(mktemp -t daily-ledger-network-guard.XXXXXX.log)"
trap 'rm -f -- "$GUARD_LOG"' EXIT
export PYTEST_NETWORK_GUARD_LOG="$GUARD_LOG"
export PYTHONPATH="$ROOT/dashboard:$ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT"

echo "== Backend tests (external network blocked) =="
"$PYTHON" -m pytest -p pytest_no_external_network -q tests
if [ -s "$GUARD_LOG" ]; then
    echo "ERROR: backend tests attempted external network access" >&2
    exit 1
fi

echo "== Frontend tests =="
node --test \
    tests/test_frontend.js \
    tests/test_frontend_calendar.js \
    tests/test_frontend_runtime.js \
    tests/test_frontend_security.js

echo "== Static validation =="
"$PYTHON" -m compileall -q dashboard scripts tests
node --check dashboard/dist/index.js
for script in scripts/*.sh; do
    bash -n "$script"
done
"$PYTHON" scripts/validate-release.py

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git diff --check
fi

echo "Daily Ledger release gate passed"
