# Release procedure

This procedure freezes a tested Summarization Calendar commit into source archives that can be installed without the development machine or its Hermes data.

## Required artifacts

A release consists of:

- Git commit and annotated semantic-version tag;
- `dashboard/manifest.json` with the same version;
- source repository with MIT `LICENSE`;
- prebuilt `dashboard/dist/index.js` and `style.css`;
- `dashboard/plugin_api.py` and `dashboard/hermes_summarization_calendar/` backend package;
- install, status, rollback, and uninstall scripts;
- README, changelog, security policy, contribution guide, and this release procedure;
- backend/frontend/loader/install/release tests;
- CI workflow;
- `.tar.gz` and `.zip` source archives;
- `SHA256SUMS` and `release-manifest.json`.

Screenshots are publication assets rather than runtime dependencies. Use only synthetic or fully redacted Hermes data.

## Pre-release gate

1. Confirm feature work is frozen and the working tree is clean.
2. Confirm the manifest version is semantic and matches the intended tag.
3. Run:

   ```bash
   ./scripts/run-tests.sh
   ```

4. Run an independent security/spec review.
5. Verify the live Dashboard discovers the plugin and its backend health route.
6. Read back one real immutable session summary and one roll-up artifact.
7. Exercise uninstall/rollback/reinstall in a disposable `HERMES_HOME`; generated data must remain.
8. Audit tracked files and release archives for credentials, local state, logs, captures, and operator-specific paths.

## Tag and build

Create the annotated tag only after the gate is green. Use the release version
being published; the example below shows the current `v1.2.3` release:

```bash
VERSION=1.2.3
git tag -a "v${VERSION}" -m "Hermes Summarization Calendar v${VERSION}"
./scripts/build-release.sh --ref "v${VERSION}"
```

The builder refuses a dirty tree, validates the selected Git ref, writes artifacts under `artifacts/`, and verifies both archives before reporting success.

## Clean-room install verification

Use a temporary Hermes home and the extracted archive. Set `VERSION` to the
release being verified:

```bash
VERSION=1.2.3
workdir="$(mktemp -d)"
tar -xzf "artifacts/hermes-summarization-calendar-v${VERSION}.tar.gz" -C "$workdir"
HERMES_HOME="$workdir/hermes-home" \
  "$workdir/hermes-summarization-calendar-v${VERSION}/scripts/install-local.sh" --copy
HERMES_HOME="$workdir/hermes-home" \
  "$workdir/hermes-summarization-calendar-v${VERSION}/scripts/status-local.sh"
```

Then import `dashboard/plugin_api.py` by file path with the actual Dashboard interpreter and verify its route set. Do not use a test-only `PYTHONPATH` for that loader check.

## Publish

A release archive contains only the selected source snapshot; it does not contain Git history. Before publishing an existing development repository, audit every commit reachable from the public branch. If that history contains operator paths, private backend names, captures, or other local details, create the public repository from the verified extracted archive instead:

```bash
VERSION=1.2.3
cd "/path/to/extracted/hermes-summarization-calendar-v${VERSION}"
git init -b main
git add .
git commit -m "release: Hermes Summarization Calendar v${VERSION}"
git tag -a "v${VERSION}" -m "Hermes Summarization Calendar v${VERSION}"
```

Before publishing:

- verify `sha256sum -c artifacts/SHA256SUMS`;
- inspect `artifacts/release-manifest.json`;
- extract each archive and confirm the top-level directory is versioned;
- confirm the Git host release points to the annotated tag;
- upload both archives, `SHA256SUMS`, and `release-manifest.json`;
- add release notes from `CHANGELOG.md`;
- add only synthetic/redacted screenshots;
- do not upload `$HERMES_HOME`, model captures, or local backup snapshots.

No remote repository or release is created by the build script.
