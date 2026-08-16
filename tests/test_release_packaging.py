"""Release packaging contract for the community-shareable v1.1 snapshot."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIRED_RELEASE_FILES = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "requirements-dev.txt",
    "docs/RELEASE.md",
    ".github/workflows/ci.yml",
    "scripts/build-release.sh",
    "scripts/run-tests.sh",
    "scripts/validate-release.py",
    "scripts/pytest_no_external_network.py",
    "dashboard/manifest.json",
    "dashboard/dist/index.js",
    "dashboard/dist/style.css",
    "dashboard/plugin_api.py",
]
PUBLIC_DOCS = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/RELEASE.md",
]


def test_required_release_files_exist() -> None:
    missing = [path for path in REQUIRED_RELEASE_FILES if not (REPO / path).is_file()]
    assert not missing, f"missing release files: {missing}"


def test_manifest_declares_v1_semver_and_expected_runtime_files() -> None:
    manifest = json.loads((REPO / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "summarization-calendar"
    assert manifest["version"] == "1.2.3"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    for field in ("entry", "css", "api"):
        assert (REPO / "dashboard" / manifest[field]).is_file(), field


def test_release_scripts_are_executable() -> None:
    for relative in ("scripts/build-release.sh", "scripts/run-tests.sh"):
        assert os.access(REPO / relative, os.X_OK), relative


def test_public_docs_have_no_operator_specific_paths_or_backend_names() -> None:
    forbidden = (
        "/home/operator",
        "private-backend.example",
        "private-model-name",
        "private-profile-name",
    )
    for relative in PUBLIC_DOCS:
        text = (REPO / relative).read_text(encoding="utf-8")
        hits = [term for term in forbidden if term in text]
        assert not hits, f"{relative} contains operator-specific terms: {hits}"


def test_bundle_is_prebuilt_sdk_iife() -> None:
    source = (REPO / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")
    assert source.lstrip().startswith("(function ()")
    assert "window.__HERMES_PLUGIN_SDK__" in source
    assert 'register(\'summarization-calendar\'' in source or 'register("summarization-calendar"' in source
