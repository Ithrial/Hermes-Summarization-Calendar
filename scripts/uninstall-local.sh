#!/usr/bin/env bash
# uninstall-local.sh — Remove daily-ledger plugin while preserving ledger data.
#
# Snapshots current state with manifest+payload layout before removal.
# Usage: ./uninstall-local.sh [--remove-data]
#   By default preserves ~/.hermes/daily-ledger contents.
#   --remove-data also removes the ledger data directory (destructive).
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/daily-ledger"
LEDGER_DIR="$HERMES_HOME/daily-ledger"
BACKUP_ROOT="$HERMES_HOME/backups/daily-ledger-install"

REMOVE_DATA="${1:---keep-data}"

ts() { date -u '+%Y%m%dT%H%M%SZ'; }
iso_ts() { date -u '+%Y-%m-%dT%H:%M:%S%z'; }

die() { echo "ERROR: $*" >&2; exit 1; }

# snapshot_target — records exact type into payload/ without following symlinks
snapshot_target() {
    local src="$1" dest="$2" prev_type="" prev_target=""

    if [ -L "$src" ]; then
        prev_type="symlink"
        prev_target="$(readlink "$src")"
    elif [ -d "$src" ]; then
        prev_type="directory"
    elif [ -f "$src" ]; then
        prev_type="file"
    else
        echo "WARNING: $src does not exist — nothing to snapshot" >&2
        return 0
    fi

    mkdir -p "$dest/payload"

    case "$prev_type" in
        symlink)
            echo "$prev_target" > "$dest/payload/link_target.txt"
            ;;
        directory)
            # A failed snapshot must abort before the installed plugin is removed.
            cp -a --no-dereference -- "$src/." "$dest/payload/"
            ;;
        file)
            cp --no-dereference -- "$src" "$dest/payload/source"
            ;;
    esac

    python3 -c "
import json, sys
data = {
    'backup_id': sys.argv[1],
    'created_at': sys.argv[2],
    'source_path': sys.argv[3],
    'previous_type': sys.argv[4],
    'previous_target': sys.argv[5],
    'snapshot_reason': 'uninstall-pre-snapshot',
    'ledger_preserved': True,
    'hermes_home': sys.argv[6],
    'payload_dir': 'payload/'
}
with open(sys.argv[7], 'w', encoding='utf-8') as handle:
    json.dump(data, handle, indent=2)
" "$(basename "$dest")" "$(iso_ts)" \
  "$(realpath -s "$src" 2>/dev/null || echo "$src")" \
  "$prev_type" "$prev_target" "$HERMES_HOME" "$dest/manifest.json"

    echo "Uninstall snapshot saved to: $dest"
}

echo "=== Daily Ledger Plugin Uninstall ==="

# Validate plugin exists
if [ ! -e "$PLUGIN_DIR" ] && [ ! -L "$PLUGIN_DIR" ]; then
    die "Plugin not found at $PLUGIN_DIR — nothing to uninstall"
fi

# Refuse unsafe paths
RESOLVED="$(readlink -f "$PLUGIN_DIR" 2>/dev/null || echo "$PLUGIN_DIR")"
case "$RESOLVED" in
    /usr/*|/etc/*|/bin/*|/sbin/*) die "Refusing to uninstall from system path: $RESOLVED" ;;
esac

# Snapshot before removal
ID="$(ts)-$$"
mkdir -p "$BACKUP_ROOT"
snapshot_target "$PLUGIN_DIR" "$BACKUP_ROOT/uninstall-$ID"

# Remove plugin
if [ -L "$PLUGIN_DIR" ]; then
    rm -- "$PLUGIN_DIR"
    echo "Removed plugin symlink: $PLUGIN_DIR"
elif [ -d "$PLUGIN_DIR" ]; then
    rm -rf -- "$PLUGIN_DIR"
    echo "Removed plugin directory: $PLUGIN_DIR"
elif [ -f "$PLUGIN_DIR" ]; then
    rm -- "$PLUGIN_DIR"
    echo "Removed plugin file: $PLUGIN_DIR"
fi

# Handle ledger data
case "$REMOVE_DATA" in
    --remove-data|-r)
        if [ -d "$LEDGER_DIR" ]; then
            rm -rf -- "$LEDGER_DIR"
            echo "Removed ledger data: $LEDGER_DIR"
        fi
        ;;
    *)
        echo ""
        echo "Ledger data PRESERVED at: $LEDGER_DIR"
        if [ -d "$LEDGER_DIR" ]; then
            python3 - "$LEDGER_DIR" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])

def count_meta(relative: str) -> int:
    base = root / relative
    return sum(1 for _ in base.rglob("meta.json")) if base.is_dir() else 0

print(f"  Session versions preserved: {count_meta('session-versions')}")
print(f"  Roll-up versions preserved: {count_meta('rollup-versions')}")
PY
        fi
        ;;
esac

echo ""
echo "=== Uninstall complete ==="
