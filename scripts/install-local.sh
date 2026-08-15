#!/usr/bin/env bash
# install-local.sh — Install summarization-calendar plugin with pre-install snapshot.
#
# Layout: <backup>/manifest.json + <backup>/payload/ (never writes through symlinks)
#   - manifest.json: machine-readable backup metadata
#   - payload/: exact copy of files/dirs; symlinks recorded but NOT followed
#
# Usage: ./install-local.sh [--symlink|--copy]
#   --symlink  (default) creates a symlink from ~/.hermes/plugins/summarization-calendar
#              pointing to the source directory.
#   --copy     creates a real copy instead.
#
# Always snapshots any pre-existing plugin target into
# ~/.hermes/backups/summarization-calendar-install/<id>/ with manifest+payload layout.
# Never touches ~/.hermes/summarization-calendar data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$(cd "$SCRIPT_DIR/.." && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/summarization-calendar"
BACKUP_ROOT="$HERMES_HOME/backups/summarization-calendar-install"
LEDGER_DIR="$HERMES_HOME/summarization-calendar"
# v1.1.0-and-earlier install locations (rename migration + backup discovery).
LEGACY_PLUGIN_DIR="$HERMES_HOME/plugins/daily-ledger"
LEGACY_BACKUP_ROOT="$HERMES_HOME/backups/daily-ledger-install"
LEGACY_LEDGER_DIR="$HERMES_HOME/daily-ledger"

INSTALL_MODE="${1:---symlink}"

# Validate and normalize mode before snapshotting or touching an existing plugin.
case "$INSTALL_MODE" in
    --symlink|-s) INSTALL_MODE="--symlink" ;;
    --copy|-c) INSTALL_MODE="--copy" ;;
    *) echo "ERROR: Unknown mode '$INSTALL_MODE'. Use --symlink or --copy." >&2; exit 1 ;;
esac

# --- helpers ---
ts() { date -u '+%Y%m%dT%H%M%SZ'; }
iso_ts() { date -u '+%Y-%m-%dT%H:%M:%S%z'; }

die() { echo "ERROR: $*" >&2; exit 1; }

# Validate that a path is safe (not root, not /usr, etc.)
validate_path_safety() {
    local target="$1" label="$2"
    # Resolve the actual path
    local resolved
    if [ -L "$target" ]; then
        resolved="$(readlink -f "$target" 2>/dev/null || echo "$target")"
    elif [ -e "$target" ]; then
        resolved="$(cd "$target" && pwd)"
    else
        resolved="$target"
    fi
    case "$resolved" in
        /|/usr/*|/etc/*|/bin/*|/sbin/*|/var/*|/boot/*|/dev/*|/proc/*|/sys/*)
            die "Unsafe $label path: $resolved (refusing to operate on system dirs)"
            ;;
    esac
}

validate_safe() {
    local target="$1" label="$2"
    local resolved
    if [ -L "$target" ]; then
        resolved="$(readlink -f "$target" 2>/dev/null || echo "$target")"
    elif [ -e "$target" ]; then
        resolved="$(cd "$target" && pwd)"
    else
        resolved="$target"
    fi
    case "$resolved" in
        "$HERMES_HOME"*|"$PLUGIN_SRC"*) return 0 ;;
        *) die "Unsafe $label path: $resolved (must be under HERMES_HOME or source)" ;;
    esac
}

# JSON-encode a string safely using Python (avoids shell interpolation issues)
json_encode() {
    python3 -c "import json, sys; print(json.dumps(sys.argv[1]))" "$1"
}

# snapshot_target SRC BACKUP_DIR
# Records exact type (dir/file/symlink) into payload/ without following symlinks.
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
        echo "WARNING: $src does not exist and is not a symlink — skipping snapshot" >&2
        return 0
    fi

    mkdir -p "$dest/payload"

    case "$prev_type" in
        symlink)
            # Record the exact target; do NOT follow or write through it.
            echo "$prev_target" > "$dest/payload/link_target.txt"
            ;;
        directory)
            # Copy directory contents into payload/ (no following symlinks inside)
            # Remove || true — if this fails, backup must abort before removing plugin
            cp -a --no-dereference -- "$src/." "$dest/payload/"
            ;;
        file)
            cp --no-dereference -- "$src" "$dest/payload/source"
            ;;
    esac

    # Write THIS backup's manifest AFTER payload so it can't contaminate symlink targets
    python3 -c "
import json, sys
data = {
    'backup_id': sys.argv[1],
    'created_at': sys.argv[2],
    'source_path': sys.argv[3],
    'previous_type': sys.argv[4],
    'previous_target': sys.argv[5],
    'ledger_preserved': True,
    'hermes_home': sys.argv[6],
    'payload_dir': 'payload/'
}
with open(sys.argv[7], 'w') as f:
    json.dump(data, f, indent=2)
" "$(basename "$dest")" "$(iso_ts)" "$(realpath -s "$src" 2>/dev/null || echo "$src")" \
  "$prev_type" "$prev_target" "$HERMES_HOME" "$dest/manifest.json"

    # Handle broken symlinks specially: if src is a broken symlink, cp -a would fail.
    # We already recorded the target above, so that's fine.
    if [ -L "$src" ] && [ ! -e "$src" ]; then
        echo "NOTE: $src is a broken symlink (target: $prev_target) — recorded in payload/link_target.txt" >&2
    fi

    echo "Snapshot saved to: $dest"
}

# --- main ---
echo "=== Summarization Calendar Plugin Install ==="
echo "Source:     $PLUGIN_SRC"
echo "Plugin dir: $PLUGIN_DIR"
echo "Mode:       $INSTALL_MODE"
echo ""

# Validate HERMES_HOME safety
validate_path_safety "$HERMES_HOME" "HERMES_HOME"
validate_path_safety "$PLUGIN_DIR" "plugin_dir"

# 1a. Migrate any legacy v1.1.0-and-earlier install (plugins/daily-ledger).
# The legacy install is superseded by the v1.2.0 rename: it is snapshotted
# into the new backup root and removed so it cannot coexist (and double-load)
# alongside the renamed plugin. It is fully recoverable via
# rollback-local.sh <id>.  Ledger DATA is never touched here — the backend
# follows the pre-existing store in place (see recap_storage.get_ledger_root).
if [ -e "$LEGACY_PLUGIN_DIR" ] || [ -L "$LEGACY_PLUGIN_DIR" ]; then
    echo "Found legacy pre-rename install at $LEGACY_PLUGIN_DIR — migrating..."
    validate_path_safety "$LEGACY_PLUGIN_DIR" "legacy_plugin_dir"
    # Only validate paths that are real directories (symlinks we just record target)
    if [ ! -L "$LEGACY_PLUGIN_DIR" ] && [ -d "$LEGACY_PLUGIN_DIR" ]; then
        validate_safe "$LEGACY_PLUGIN_DIR" "legacy_plugin"
    fi
    LEGACY_ID="legacy-migration-$(ts)-$$"
    LEGACY_BACKUP_PATH="$BACKUP_ROOT/$LEGACY_ID"
    mkdir -p "$BACKUP_ROOT"
    snapshot_target "$LEGACY_PLUGIN_DIR" "$LEGACY_BACKUP_PATH"
    if [ -L "$LEGACY_PLUGIN_DIR" ]; then
        rm -- "$LEGACY_PLUGIN_DIR"
    elif [ -d "$LEGACY_PLUGIN_DIR" ]; then
        rm -rf -- "$LEGACY_PLUGIN_DIR"
    else
        rm -- "$LEGACY_PLUGIN_DIR"
    fi
    echo "Legacy install backed up to: $LEGACY_BACKUP_PATH"
    echo "Restore it with: rollback-local.sh $LEGACY_ID"
    echo ""
fi

# 1. Build the replacement at a sibling staging path before touching current.
PLUGIN_PARENT="$(dirname "$PLUGIN_DIR")"
mkdir -p "$PLUGIN_PARENT"
STAGE="$PLUGIN_PARENT/.summarization-calendar-install-stage-$$"
OLD="$PLUGIN_PARENT/.summarization-calendar-install-old-$$"
rm -rf -- "$STAGE" "$OLD"
# Never remove OLD from an EXIT trap: if both the swap and automatic restore
# fail, OLD is the only surviving copy and must remain recoverable.
cleanup() { rm -rf -- "$STAGE"; }
trap cleanup EXIT

case "$INSTALL_MODE" in
    --symlink)
        ln -s -- "$PLUGIN_SRC" "$STAGE"
        ;;
    --copy)
        cp -a -- "$PLUGIN_SRC" "$STAGE"
        ;;
esac

# 2. Snapshot pre-existing plugin if present (file, dir, or symlink including broken)
if [ -e "$PLUGIN_DIR" ] || [ -L "$PLUGIN_DIR" ]; then
    echo "Found existing plugin at $PLUGIN_DIR — creating backup..."

    # Only validate paths that are real directories (symlinks we just record target)
    if [ ! -L "$PLUGIN_DIR" ] && [ -d "$PLUGIN_DIR" ]; then
        validate_safe "$PLUGIN_DIR" "plugin"
    fi

    ID="$(ts)-$$"
    BACKUP_PATH="$BACKUP_ROOT/$ID"
    mkdir -p "$BACKUP_ROOT"

    snapshot_target "$PLUGIN_DIR" "$BACKUP_PATH"

    echo "Manifest: $BACKUP_PATH/manifest.json"

else
    echo "No existing plugin — clean install."
fi

# 3. Swap staged replacement into place; restore current on move failure.
if [ -e "$PLUGIN_DIR" ] || [ -L "$PLUGIN_DIR" ]; then
    mv -T -- "$PLUGIN_DIR" "$OLD"
fi
if ! mv -T -- "$STAGE" "$PLUGIN_DIR"; then
    if [ -e "$OLD" ] || [ -L "$OLD" ]; then
        mv -T -- "$OLD" "$PLUGIN_DIR"
    fi
    die "Install swap failed; previous plugin restored"
fi
rm -rf -- "$OLD"

if [ "$INSTALL_MODE" = "--symlink" ]; then
    echo "Symlink created: $PLUGIN_DIR -> $PLUGIN_SRC"
else
    echo "Copied to: $PLUGIN_DIR"
fi

# 4. Ensure ledger dirs exist (but never touch existing data)
mkdir -p "$LEDGER_DIR/recaps" "$LEDGER_DIR/versions" "$LEDGER_DIR/running"

# 5. Report the effective ledger data dir. If a pre-rename store with data
# exists at the legacy location, the backend follows it in place (see
# recap_storage.get_ledger_root) — report that, not the fresh scaffold.
_effective_ledger_dir() {
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
EFFECTIVE_LEDGER="$(_effective_ledger_dir)"

echo ""
echo "=== Install complete ==="
if [ "$EFFECTIVE_LEDGER" = "$LEGACY_LEDGER_DIR" ]; then
    echo "Ledger data dir: $EFFECTIVE_LEDGER (existing pre-rename data, kept in place)"
else
    echo "Ledger data dir: $EFFECTIVE_LEDGER"
fi
echo "Restart Hermes Dashboard to load the plugin."
