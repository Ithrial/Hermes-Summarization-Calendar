#!/usr/bin/env python3
"""Validate the source tree as a shareable Summarization Calendar release."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "requirements-dev.txt",
    "docs/RELEASE.md",
    ".github/workflows/ci.yml",
    "dashboard/manifest.json",
    "dashboard/dist/index.js",
    "dashboard/dist/style.css",
    "dashboard/plugin_api.py",
    "dashboard/hermes_summarization_calendar/__init__.py",
    "scripts/install-local.sh",
    "scripts/status-local.sh",
    "scripts/rollback-local.sh",
    "scripts/uninstall-local.sh",
    "scripts/run-tests.sh",
    "scripts/build-release.sh",
    "scripts/pytest_no_external_network.py",
)
PUBLIC_TEXT = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "PROJECT-BRIEF.md",
    "AGENTS.md",
    "docs/RELEASE.md",
    "docs/plans/2026-07-26-summarization-calendar-implementation.md",
    "docs/plans/2026-07-27-session-summaries.md",
)
FORBIDDEN_TERMS = (
    "/home/operator",
    "private-backend.example",
    "private-model-name",
    "private-profile-name",
)
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(\.env|auth\.json|state\.db|executions\.db|id_rsa|id_ed25519|.*\.(pem|key|sqlite|sqlite3|log))$",
    re.IGNORECASE,
)
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password)\b"
        r"\s*[:=]\s*[\"'][^\"'${<]{12,}[\"']"
    ),
)


def tracked_files() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*")
            if path.is_file() and ".git" not in path.parts
        ]
    return [line for line in result.stdout.splitlines() if line]


def validate(expected_version: str | None = None) -> list[str]:
    errors: list[str] = []

    missing = [relative for relative in REQUIRED if not (ROOT / relative).is_file()]
    if missing:
        errors.append(f"missing required release files: {', '.join(missing)}")

    manifest_path = ROOT / "dashboard" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid dashboard manifest: {exc}")
        manifest = {}

    version = str(manifest.get("version", ""))
    if not SEMVER.fullmatch(version):
        errors.append(f"manifest version is not semantic: {version!r}")
    if expected_version and version != expected_version:
        errors.append(
            f"manifest version {version!r} does not match expected {expected_version!r}"
        )
    if manifest.get("name") != "summarization-calendar":
        errors.append("manifest name must be 'summarization-calendar'")

    for field in ("entry", "css", "api"):
        relative = manifest.get(field)
        if not isinstance(relative, str) or not (manifest_path.parent / relative).is_file():
            errors.append(f"manifest field {field!r} does not resolve to a file")

    bundle_path = ROOT / "dashboard" / "dist" / "index.js"
    if bundle_path.is_file():
        bundle = bundle_path.read_text(encoding="utf-8")
        if "window.__HERMES_PLUGIN_SDK__" not in bundle:
            errors.append("frontend bundle does not use the Hermes Plugin SDK")
        if not re.search(r"register\([\"']summarization-calendar[\"']", bundle):
            errors.append("frontend bundle does not register the manifest name")

    for relative in PUBLIC_TEXT:
        path = ROOT / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for term in FORBIDDEN_TERMS:
            if term in text:
                errors.append(f"operator-specific term {term!r} in {relative}")

    tracked = tracked_files()
    bad_names = [relative for relative in tracked if FORBIDDEN_NAMES.search(relative)]
    if bad_names:
        errors.append(f"tracked private/runtime filenames: {', '.join(bad_names)}")

    for relative in tracked:
        if relative.startswith("artifacts/") or "__pycache__" in relative:
            errors.append(f"generated artifact is tracked: {relative}")

        if relative == "scripts/validate-release.py":
            continue
        path = ROOT / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            errors.append(f"possible credential material in tracked file: {relative}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="require this exact manifest version")
    args = parser.parse_args()

    errors = validate(args.version)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    manifest = json.loads(
        (ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")
    )
    print(f"Release validation passed for summarization-calendar v{manifest['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
