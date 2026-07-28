#!/usr/bin/env bash
# status-local.sh — Show install status of daily-ledger plugin.
#
# Usage: ./status-local.sh
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/daily-ledger"
LEDGER_DIR="$HERMES_HOME/daily-ledger"
BACKUP_ROOT="$HERMES_HOME/backups/daily-ledger-install"

echo "=== Daily Ledger Plugin Status ==="
echo ""

# Plugin install status
if [ -L "$PLUGIN_DIR" ]; then
    TARGET="$(readlink "$PLUGIN_DIR")"
    echo "Plugin:   symlink -> $TARGET"
elif [ -d "$PLUGIN_DIR" ]; then
    echo "Plugin:   directory at $PLUGIN_DIR"
else
    echo "Plugin:   NOT INSTALLED"
fi

echo ""

# Ledger data
if [ -d "$LEDGER_DIR" ]; then
    echo "Ledger:   $LEDGER_DIR"
    python3 - "$LEDGER_DIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])

def count_meta(relative: str) -> int:
    base = root / relative
    return sum(1 for _ in base.rglob("meta.json")) if base.is_dir() else 0

active = 0
running = root / "running"
if running.is_dir():
    for path in running.rglob("*.json"):
        try:
            status = json.loads(path.read_text(encoding="utf-8")).get("status")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if status in {"queued", "running"}:
            active += 1

print(f"  Session versions: {count_meta('session-versions')}")
print(f"  Roll-up versions: {count_meta('rollup-versions')}")
print(f"  Active jobs: {active}")
PY
else
    echo "Ledger:   NOT INITIALIZED"
fi

echo ""

# Backups
if [ -d "$BACKUP_ROOT" ]; then
    BACKUPS=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    echo "Backups:  $BACKUPS snapshots"
    for d in "$BACKUP_ROOT"/*/; do
        [ -d "$d" ] || continue
        ID="$(basename "$d")"
        if [ -f "$d/manifest.json" ]; then
            TYPE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('previous_type','?'))" "$d/manifest.json" 2>/dev/null || echo '?')
            echo "  $ID ($TYPE)"
        fi
    done
else
    echo "Backups:  none"
fi

echo ""
