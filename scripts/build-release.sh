#!/usr/bin/env bash
# Build verified source archives from a clean committed Daily Ledger ref.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="HEAD"
OUTPUT="$ROOT/artifacts"

usage() {
    echo "Usage: $0 [--ref GIT_REF] [--output DIR]"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --ref)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            REF="$2"
            shift 2
            ;;
        --output)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            OUTPUT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

cd "$ROOT"
git rev-parse --verify "${REF}^{commit}" >/dev/null

if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
    echo "ERROR: release build requires a clean working tree" >&2
    exit 1
fi

REF_COMMIT="$(git rev-parse "${REF}^{commit}")"
HEAD_COMMIT="$(git rev-parse HEAD)"
if [ "$REF_COMMIT" != "$HEAD_COMMIT" ]; then
    echo "ERROR: selected ref must resolve to the checked-out clean HEAD" >&2
    exit 1
fi

VERSION="$(git show "$REF:dashboard/manifest.json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
python3 scripts/validate-release.py --version "$VERSION"

PREFIX="hermes-daily-ledger-v$VERSION"
mkdir -p "$OUTPUT"
TAR_PATH="$OUTPUT/$PREFIX.tar.gz"
ZIP_PATH="$OUTPUT/$PREFIX.zip"
CHECKSUMS="$OUTPUT/SHA256SUMS"
MANIFEST="$OUTPUT/release-manifest.json"

rm -f -- "$TAR_PATH" "$ZIP_PATH" "$CHECKSUMS" "$MANIFEST"

git archive --format=tar --prefix="$PREFIX/" "$REF" | gzip -n > "$TAR_PATH"
git archive --format=zip --prefix="$PREFIX/" -o "$ZIP_PATH" "$REF"

(
    cd "$OUTPUT"
    sha256sum "$(basename "$TAR_PATH")" "$(basename "$ZIP_PATH")" > "$(basename "$CHECKSUMS")"
)

python3 - "$VERSION" "$REF" "$REF_COMMIT" "$OUTPUT" "$MANIFEST" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

version, ref, commit, output, target = sys.argv[1:]
output_path = Path(output)
files = []
for archive in sorted(output_path.glob(f"hermes-daily-ledger-v{version}.*")):
    if archive.suffix not in {".gz", ".zip"}:
        continue
    files.append({
        "name": archive.name,
        "bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    })
manifest = {
    "name": "daily-ledger",
    "version": version,
    "source_ref": ref,
    "commit": commit,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "files": files,
}
Path(target).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

(
    cd "$OUTPUT"
    sha256sum -c "$(basename "$CHECKSUMS")"
)

TMP="$(mktemp -d -t daily-ledger-release.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
tar -xzf "$TAR_PATH" -C "$TMP"
python3 - "$TMP/$PREFIX/dashboard/manifest.json" "$VERSION" <<'PY'
import json
from pathlib import Path
import sys
manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert manifest["version"] == sys.argv[2]
assert manifest["name"] == "daily-ledger"
PY
python3 - "$ZIP_PATH" "$PREFIX/dashboard/manifest.json" "$VERSION" <<'PY'
import json
import sys
import zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    manifest = json.loads(archive.read(sys.argv[2]))
assert manifest["version"] == sys.argv[3]
assert manifest["name"] == "daily-ledger"
PY

echo "Release artifacts built in: $OUTPUT"
echo "  $(basename "$TAR_PATH")"
echo "  $(basename "$ZIP_PATH")"
echo "  $(basename "$CHECKSUMS")"
echo "  $(basename "$MANIFEST")"
