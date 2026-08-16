# Changelog

All notable changes to Hermes Summarization Calendar are documented here.

## 1.2.3 — 2026-08-16

### Documentation
- Documented the complete checksum-verified release update flow, atomic plugin replacement, backup/rollback behavior, and Dashboard restart step.
- Updated active release procedures and compatibility statements from v1.0/v1.1 to the current release line.
- Marked historical release notes and implementation plans so legacy names and design contracts are not mistaken for current behavior.

## 1.2.2 — 2026-08-16

### Security
- Prevented batch-creation failures from returning raw internal exception text, filesystem paths, or credential-like values in API responses.
- Added a regression test covering path and credential-like value disclosure.
- Corrected the security policy's supported-version, sanitization, Windows-scope, and private-reporting guidance.

## 1.2.1 — 2026-08-16

### Fixed
- Made batch finalization idempotent so a coordinator retry or concurrent finalizer returns immutable terminal state instead of surfacing a misleading failure.
- Replaced the batch worker's `None`/`unknown` failure diagnostic with the failed-member count for normal all-failed batches.
- Resolved generated ledger roots from the active `HERMES_HOME` at runtime while preserving the explicit `LEDGER_ROOT` override and legacy `daily-ledger` fallback.

### Documentation
- Clarified that `AGENTS.md` restrictions apply to automated workers, not maintainer-directed installation, rollback, restart, or publication.
- Documented the exact `plugins.enabled` manifest-name requirement for standalone Dashboard extensions.

## 1.2.0 — 2026-08-15

### Changed
- Renamed the plugin identity from "Daily Ledger" to "Hermes Summarization Calendar". The plugin id, API route, Python package, install directory, data root, archive prefix, and CI artifact are now all `summarization-calendar` (package `hermes_summarization_calendar`). This is a cosmetic-plus-path rename: the feature set is unchanged from 1.1.0.

### Compatibility
- Backward compatible with v1.1.0-and-earlier data. Existing installs keep their recaps:
  - **Data root fallback** — if `$HERMES_HOME/summarization-calendar` has no stored data but the legacy `$HERMES_HOME/daily-ledger` does, the backend follows the legacy store in place (no split-brain, no copy). Fresh installs land on the new root.
  - **Legacy source exclusion** — plugin-internal sessions tagged `source='daily-ledger'` (written by pre-rename Hermes core) are still excluded from inventory and recap input, alongside the current `source='summarization-calendar'` tag.
  - **Legacy install migration** — `install-local.sh` snapshots and removes any pre-rename `plugins/daily-ledger` install (into the new backup root) so it cannot coexist or double-load; it is fully restorable via `rollback-local.sh`.
  - **Legacy backup discovery** — `rollback-local.sh` lists and restores v1.1.0-era backups found under the legacy `backups/daily-ledger-install` root.
  - **Legacy UI preference** — the browser `showAutoTitled` preference written under the old `hermes.daily-ledger.showAutoTitled` localStorage key is still honored (read-only fallback; toggling writes the new key).
- `status-local.sh` reports the *effective* data store (new root, or the legacy root if that is what holds data).
- Day boundaries remain `America/Chicago`.

## 1.1.0 — 2026-08-02

### Added
- Exact profile-scoped session links in the Calendar dashboard.
- Durable server-side batch generator for independent session summaries with persistent status, sequential processing, refresh recovery, and immutable history/rollback retained.
- Restored session model metadata in the detail row.
- Browser-local persisted "Show agent-generated sessions" filter with exact fallback-title classification; canonical counts and roll-up coverage remain unchanged; hidden sessions excluded from selection and submission.

### Improved/Fixed
- Calendar titles navigate directly to `/chat?resume=<session_id>&profile=<profile>` with sanitized text and exact URL encoding.
- Per-card generation buttons replaced by shared batch toolbar; one batch request handles multiple sessions sequentially through the existing per-session pipeline.
- Auto-titled session filter (`Session YYYYMMDD_HHMMSS_<identifier>`) defaults to visible and persists locally; toggling off hides rows without affecting canonical inventory or artifacts.

### Compatibility
- Backward compatible with v1.0 API routes and artifacts; new batch and visibility features are additive.
- Day boundaries remain `America/Chicago`.
- Existing session summaries, roll-ups, and immutable version history remain readable and unmodified.

### Upgrade/Rollback
- Upgrade: Extract release archive and run `install-local.sh --copy`; restart the Hermes Dashboard.
- Rollback: Use `rollback-local.sh <backup-id>` from the pre-upgrade snapshot or re-install v1.0.0 archive; restart the Dashboard.
- Generated ledger data is preserved during both upgrade and rollback.

### Verification
- All v1.0 backend tests pass.
- New batch and frontend visibility tests added; full release gate passes with no external network access.
- Archive integrity and structure verified.

## 1.0.0 — 2026-07-28

First stable, community-shareable release.

### Added

- Calendar Dashboard page with month navigation and per-day Hermes activity.
- Multi-profile session inventory and cron execution inventory.
- Manual per-session summary generation through `auxiliary.compression`.
- Lossless transcript chunking and deterministic multi-chunk reduction.
- Optional summary-only daily roll-ups with explicit coverage.
- Immutable JSON and Markdown artifact versions, source fingerprints, and staleness detection.
- Durable queued/running/completed/failed job state.
- Atomic publication, rollback, install, status, and uninstall tooling.
- Provider-enforced JSON-object requests with a bounded one-terminal-closer compatibility repair.
- Backend, frontend, loader-contract, security, storage, installation, and release tests.

### Compatibility

- Tested with Hermes Agent v0.19.0 (2026.7.20) and Python 3.11.
- Linux is the supported v1.0 runtime platform.
- Day boundaries are fixed to `America/Chicago` in v1.0.
