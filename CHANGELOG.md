# Changelog

All notable changes to Hermes Daily Ledger are documented here.

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
