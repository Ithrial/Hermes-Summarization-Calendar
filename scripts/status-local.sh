#!/usr/bin/env bash
# status-local.sh — Show install status of summarization-calendar plugin.
#
# Usage: ./status-local.sh
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/summarization-calendar"
LEDGER_DIR="$HERMES_HOME/summarization-calendar"
LEGACY_LEDGER_DIR="$HERMES_HOME/daily-ledger"
BACKUP_ROOT="$HERMES_HOME/backups/summarization-calendar-install"

echo "=== Summarization Calendar Plugin Status ==="
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

# Ledger data. Report the *effective* store (same resolution as
# recap_storage.get_ledger_root): LEDGER_ROOT env, else the new root if it
# has data, else the legacy pre-rename root if it has data.
_effective_ledger_dir() {
    if [ -n "${LEDGER_ROOT:-}" ]; then
        printf '%s\n' "$LEDGER_ROOT"
        return 0
    fi
    local d sub
    for d in "$LEDGER_DIR" "$LEGACY_LEDGER_DIR"; do
        for sub in recaps versions session-versions rollup-versions; do
            if [ -d "$d/$sub" ] && [ -n "$(ls -A -- "$d/$sub" 2>/dev/null | head -n 1)" ]; then
                printf '%s\n' "$d"
                return 0
            fi
        done
    done
    printf '%s\n' "$LEDGER_DIR"
}
EFFECTIVE_LEDGER_DIR="$(_effective_ledger_dir)"
if [ -d "$EFFECTIVE_LEDGER_DIR" ]; then
    if [ "$EFFECTIVE_LEDGER_DIR" = "$LEGACY_LEDGER_DIR" ]; then
        echo "Ledger:   $EFFECTIVE_LEDGER_DIR (existing pre-rename data, kept in place)"
    else
        echo "Ledger:   $EFFECTIVE_LEDGER_DIR"
    fi
    python3 - "$EFFECTIVE_LEDGER_DIR" <<'PY'
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
