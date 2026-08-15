#!/usr/bin/env bash
# rollback-local.sh — Restore a previous plugin version from backup.
#
# Supports two layouts:
#   1. New: <backup>/manifest.json + <backup>/payload/
#      Restores exact type (directory/file/symlink) from payload/.
#   2. Legacy flat: plugin files directly under backup root alongside manifest.
#      Detected by missing payload/ directory with files present at root level.
#
# Snapshots current state before rollback with same manifest+payload layout.
# Preserves ledger data. Handles broken symlinks.
# Validates BACKUP_ID against traversal/separators.
# Ensures target stays directly below BACKUP_ROOT.
# JSON-encode manifest fields safely using Python, not raw shell interpolation.
#
# Usage: ./rollback-local.sh <backup_id>
#   Lists available backups if no ID provided.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/summarization-calendar"
BACKUP_ROOT="$HERMES_HOME/backups/summarization-calendar-install"
LEGACY_BACKUP_ROOT="$HERMES_HOME/backups/daily-ledger-install"
LEDGER_DIR="$HERMES_HOME/summarization-calendar"

ts() { date -u '+%Y%m%dT%H%M%SZ'; }
iso_ts() { date -u '+%Y-%m-%dT%H:%M:%S%z'; }

die() { echo "ERROR: $*" >&2; exit 1; }

# Validate path safety (not root, /usr, etc.)
validate_path_safety() {
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
        /|/usr/*|/etc/*|/bin/*|/sbin/*|/var/*|/boot/*|/dev/*|/proc/*|/sys/*)
            die "Unsafe $label path: $resolved (refusing to operate on system dirs)"
            ;;
    esac
}

# JSON-encode a string safely using Python
json_encode() {
    python3 -c "import json, sys; print(json.dumps(sys.argv[1]))" "$1"
}

# Read a manifest field safely via Python (not shell interpolation)
read_manifest_field() {
    python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2], sys.argv[3] if len(sys.argv)>3 else ''))" "$1" "${@:2}"
}

# snapshot_target SRC BACKUP_DIR — same logic as install script
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
            # Must succeed — backup failure aborts before removing plugin
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
    'snapshot_reason': sys.argv[6],
    'ledger_preserved': True,
    'hermes_home': sys.argv[7],
    'payload_dir': 'payload/'
}
with open(sys.argv[8], 'w') as f:
    json.dump(data, f, indent=2)
" "$(basename "$dest")" "$(iso_ts)" "$(realpath -s "$src" 2>/dev/null || echo "$src")" \
  "$prev_type" "$prev_target" "rollback-pre-snapshot" "$HERMES_HOME" "$dest/manifest.json"

    echo "Current state archived to: $dest"
}

echo "=== Summarization Calendar Plugin Rollback ==="

# List available backups if none specified
if [ $# -lt 1 ]; then
    echo "Usage: $0 <backup_id>"
    echo ""
    echo "Available backups:"
    LISTED_ANY=false
    for root in "$BACKUP_ROOT" "$LEGACY_BACKUP_ROOT"; do
        [ -d "$root" ] || continue
        if [ "$root" = "$LEGACY_BACKUP_ROOT" ]; then
            echo "  (legacy pre-rename backup root: $root)"
        fi
        for d in "$root"/*/; do
            [ -d "$d" ] || continue
            manifest="$d/manifest.json"
            if [ -f "$manifest" ]; then
                id="$(basename "$d")"
                prev_type="$(read_manifest_field "$manifest" "previous_type" "?")"
                created="$(read_manifest_field "$manifest" "created_at" "?")"
                echo "  $id  (type=$prev_type, created=$created)"
                LISTED_ANY=true
            fi
        done
    done
    if [ "$LISTED_ANY" = false ]; then
        echo "  (none — no backups found)"
    fi
    exit 1
fi

BACKUP_ID="$1"

# Validate BACKUP_ID against traversal/separators
if [[ "$BACKUP_ID" == *"/"* ]] || [[ "$BACKUP_ID" == *..* ]]; then
    die "Invalid backup ID: contains path separators or traversal sequences"
fi

# Locate the backup. The new root wins on identical IDs; v1.1.0-and-earlier
# backups live under the legacy pre-rename root and remain restorable.
BACKUP_PATH=""
for root in "$BACKUP_ROOT" "$LEGACY_BACKUP_ROOT"; do
    candidate="$root/$BACKUP_ID"
    if [ -d "$candidate" ]; then
        BACKUP_PATH="$candidate"
        break
    fi
done

if [ -z "$BACKUP_PATH" ]; then
    die "Backup $BACKUP_ID not found in $BACKUP_ROOT or $LEGACY_BACKUP_ROOT"
fi
if [ -d "$LEGACY_BACKUP_ROOT" ] && [[ "$BACKUP_PATH" == "$LEGACY_BACKUP_ROOT"/* ]]; then
    echo "NOTE: restoring from legacy pre-rename backup root ($LEGACY_BACKUP_ROOT)" >&2
fi
BACKUP_ROOT_RESOLVED="$(dirname -- "$BACKUP_PATH")"

# Ensure target stays directly below its backup root (no traversal)
RESOLVED_BACKUP="$(cd -P "$BACKUP_PATH" 2>/dev/null && pwd)" || true
case "$RESOLVED_BACKUP" in
    "$BACKUP_ROOT_RESOLVED"/*) : ;; # OK — direct child
    *) die "Backup resolves outside its backup root: $RESOLVED_BACKUP" ;;
esac

# Validate HERMES_HOME/PLUGIN_DIR safety
validate_path_safety "$HERMES_HOME" "HERMES_HOME"
validate_path_safety "$(dirname "$PLUGIN_DIR")" "plugin_parent_dir"

# Read manifest — detect layout (new vs legacy flat)
MANIFEST="$BACKUP_PATH/manifest.json"
[ -f "$MANIFEST" ] || die "No manifest.json in backup: $BACKUP_PATH"
[ ! -L "$MANIFEST" ] || die "Refusing symlinked backup manifest: $MANIFEST"

PRE_TYPE="$(read_manifest_field "$MANIFEST" "previous_type" "directory")"
PRE_TARGET="$(read_manifest_field "$MANIFEST" "previous_target" "")"

# Detect legacy flat layout: no payload/ dir but files exist at backup root
PAYLOAD="$BACKUP_PATH/payload"
IS_LEGACY=false
[ ! -L "$PAYLOAD" ] || die "Refusing symlinked backup payload: $PAYLOAD"
if [ ! -d "$PAYLOAD" ]; then
    # Detect any root entry other than backup metadata, including directories
    # and symlinks.  Legacy plugins are not required to have a root-level file.
    for entry in "$BACKUP_PATH"/* "$BACKUP_PATH"/.[!.]* "$BACKUP_PATH"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        [ "$(basename "$entry")" = "manifest.json" ] && continue
        IS_LEGACY=true
        break
    done
    if [ "$IS_LEGACY" = true ]; then
        echo "NOTE: Legacy flat backup detected — restoring from backup root entries" >&2
    fi
fi

# Build the restore candidate at a sibling path before touching current.
PLUGIN_PARENT="$(dirname "$PLUGIN_DIR")"
mkdir -p "$PLUGIN_PARENT"
RESTORE_STAGE="$PLUGIN_PARENT/.summarization-calendar-restore-stage-$$"
OLD="$PLUGIN_PARENT/.summarization-calendar-restore-old-$$"
rm -rf -- "$RESTORE_STAGE" "$OLD"
# Never remove OLD from an EXIT trap: it is the recovery handle if both the
# staged swap and automatic restoration fail.
cleanup() { rm -rf -- "$RESTORE_STAGE"; }
trap cleanup EXIT

case "$PRE_TYPE" in
    symlink)
        if [ -n "$PRE_TARGET" ]; then
            ln -s -- "$PRE_TARGET" "$RESTORE_STAGE"
        elif [ "$IS_LEGACY" = false ] && [ -f "$PAYLOAD/link_target.txt" ]; then
            link_target="$(cat "$PAYLOAD/link_target.txt")"
            ln -s -- "$link_target" "$RESTORE_STAGE"
        else
            die "Symlink target unknown in manifest and no link_target.txt in payload"
        fi
        ;;
    directory)
        mkdir -p -- "$RESTORE_STAGE"
        if [ "$IS_LEGACY" = true ]; then
            cp -a --no-dereference -- "$BACKUP_PATH/." "$RESTORE_STAGE/"
            rm -f "$RESTORE_STAGE/manifest.json"
        elif [ -d "$PAYLOAD" ]; then
            # An empty payload is the exact representation of an empty directory.
            cp -a --no-dereference -- "$PAYLOAD/." "$RESTORE_STAGE/"
        else
            die "Directory backup has neither payload nor legacy entries"
        fi
        ;;
    file)
        [ -f "$PAYLOAD/source" ] || die "No source file in payload for file-type backup"
        [ ! -L "$PAYLOAD/source" ] || die "Refusing symlinked file payload"
        cp --no-dereference -- "$PAYLOAD/source" "$RESTORE_STAGE"
        ;;
    *)
        die "Unknown previous type: $PRE_TYPE"
        ;;
esac

# Archive current plugin before rollback (if it exists) using manifest+payload layout.
CURRENT_ID="$(ts)-$$"
if [ -e "$PLUGIN_DIR" ] || [ -L "$PLUGIN_DIR" ]; then
    mkdir -p "$BACKUP_ROOT"
    CURRENT_BACKUP="$BACKUP_ROOT/current-before-rollback-$CURRENT_ID"
    snapshot_target "$PLUGIN_DIR" "$CURRENT_BACKUP"
fi

# Swap the staged candidate into place; restore current on move failure.
if [ -e "$PLUGIN_DIR" ] || [ -L "$PLUGIN_DIR" ]; then
    mv -T -- "$PLUGIN_DIR" "$OLD"
fi
if ! mv -T -- "$RESTORE_STAGE" "$PLUGIN_DIR"; then
    if [ -e "$OLD" ] || [ -L "$OLD" ]; then
        mv -T -- "$OLD" "$PLUGIN_DIR"
    fi
    die "Rollback swap failed; previous plugin restored"
fi
rm -rf -- "$OLD"

case "$PRE_TYPE" in
    symlink) echo "Restored as symlink: $PLUGIN_DIR -> $(readlink "$PLUGIN_DIR")" ;;
    directory) echo "Restored as directory: $PLUGIN_DIR" ;;
    file) echo "Restored as file: $PLUGIN_DIR" ;;
esac

echo ""
echo "=== Rollback complete ==="
echo "Ledger data preserved at: $LEDGER_DIR"
echo "Restart Hermes Dashboard to load the restored plugin."
