#!/usr/bin/env bash
# uninstall-local.sh — Remove summarization-calendar plugin while preserving ledger data.
#
# Snapshots current state with manifest+payload layout before removal.
# Usage: ./uninstall-local.sh [--remove-data]
#   By default preserves ~/.hermes/summarization-calendar contents.
#   --remove-data also removes the ledger data directory (destructive).
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/summarization-calendar"
LEDGER_DIR="$HERMES_HOME/summarization-calendar"
LEGACY_LEDGER_DIR="$HERMES_HOME/daily-ledger"
BACKUP_ROOT="$HERMES_HOME/backups/summarization-calendar-install"

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

echo "=== Summarization Calendar Plugin Uninstall ==="

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

# Handle ledger data. The effective store may be the new root or the legacy
# pre-rename root (whichever holds data — see recap_storage.get_ledger_root).
# --remove-data is explicit and destructive: it removes BOTH roots so a
# --remove-data uninstall never leaves a stranded pre-rename store behind.

# Mirrors _root_has_data() in recap_storage: a root counts as holding data
# only if one of its data subdirectories is non-empty (empty scaffolds don't).
_store_has_data() {
    local root="$1" d
    for d in recaps versions session-versions rollup-versions; do
        if [ -d "$root/$d" ] && [ -n "$(ls -A "$root/$d" 2>/dev/null | head -1)" ]; then
            return 0
        fi
    done
    return 1
}

case "$REMOVE_DATA" in
    --remove-data|-r)
        if [ -d "$LEDGER_DIR" ]; then
            rm -rf -- "$LEDGER_DIR"
            echo "Removed ledger data: $LEDGER_DIR"
        fi
        if [ -d "$LEGACY_LEDGER_DIR" ]; then
            rm -rf -- "$LEGACY_LEDGER_DIR"
            echo "Removed legacy ledger data: $LEGACY_LEDGER_DIR"
        fi
        ;;
    *)
        # Report the store that actually holds data (mirrors
        # recap_storage.get_ledger_root: new root first, then legacy), so the
        # operator is told where their recaps really live.
        if _store_has_data "$LEDGER_DIR"; then
            REPORT_STORE="$LEDGER_DIR"
            REPORT_NOTE=""
        elif _store_has_data "$LEGACY_LEDGER_DIR"; then
            REPORT_STORE="$LEGACY_LEDGER_DIR"
            REPORT_NOTE=" (pre-rename store, kept in place)"
        else
            REPORT_STORE="$LEDGER_DIR"
            REPORT_NOTE=""
        fi
        echo ""
        echo "Ledger data PRESERVED at: $REPORT_STORE$REPORT_NOTE"
        python3 - "$LEDGER_DIR" "$LEGACY_LEDGER_DIR" <<'PY'
import sys
from pathlib import Path

def count_meta(root: Path, relative: str) -> int:
    base = root / relative
    return sum(1 for _ in base.rglob("meta.json")) if base.is_dir() else 0

new_root = Path(sys.argv[1])
legacy_root = Path(sys.argv[2])
session = count_meta(new_root, "session-versions") + count_meta(legacy_root, "session-versions")
rollup = count_meta(new_root, "rollup-versions") + count_meta(legacy_root, "rollup-versions")
print(f"  Session versions preserved: {session}")
print(f"  Roll-up versions preserved: {rollup}")
PY
        ;;
esac

echo ""
echo "=== Uninstall complete ==="
