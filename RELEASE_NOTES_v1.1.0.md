# Hermes Daily Ledger v1.1.0 Release Notes

## Added

- Exact profile-scoped session links in the Calendar dashboard. Clicking a session title navigates directly to `/chat?resume=<session_id>&profile=<profile>` with sanitized text and exact URL encoding.
- Durable server-side batch generator for independent session summaries. Submit one batch request to generate or regenerate multiple selected summaries sequentially through the existing per-session pipeline. Persistent status survives page refresh and restart.
- Restored session model metadata in the detail row.
- Browser-local persisted "Show agent-generated sessions" filter with exact fallback-title classification (`Session YYYYMMDD_HHMMSS_<identifier>`). Default is visible; toggling off hides rows without affecting canonical inventory or roll-up coverage.

## Improved/Fixed

- Per-card generation buttons replaced by shared batch toolbar; one batch request handles multiple sessions.
- Auto-titled session filter defaults to visible and persists locally; toggling off hides rows without affecting canonical inventory or artifacts.

## Compatibility

- Backward compatible with v1.0 API routes and artifacts; new batch and visibility features are additive.
- Day boundaries remain `America/Chicago`.
- Existing session summaries, roll-ups, and immutable version history remain readable and unmodified.

## Upgrade/Rollback

### Upgrade

1. Verify the downloaded archive integrity: `sha256sum -c SHA256SUMS`
2. Extract: `tar -xzf hermes-daily-ledger-v1.1.0.tar.gz`
3. Install: `cd hermes-daily-ledger-v1.1.0 && ./scripts/install-local.sh --copy`
4. Restart the Hermes Dashboard

### Rollback

1. Use `rollback-local.sh <backup-id>` from the pre-upgrade snapshot or re-install v1.0.0 archive
2. Restart the Hermes Dashboard

Generated ledger data is preserved during both upgrade and rollback.

## Verification

- All v1.0 backend tests pass (348 backend, 106 frontend).
- Full release gate passes with no external network access.
- Archive integrity and structure verified.
- v1.1-to-v1.0 rollback tested with ledger-data preservation.
