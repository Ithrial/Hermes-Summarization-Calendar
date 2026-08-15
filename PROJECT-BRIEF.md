# Hermes Summarization Calendar — Project Brief

## Goal

Build a standalone community-shareable Hermes Web Dashboard plugin that adds a **Calendar** sidebar page at `/calendar`.

The page automatically inventories Hermes activity by day and allows users to manually request an optional summary-model generated recap. Most days may have activity without a recap.

## Required user-visible behavior

### Calendar and daily inventory

- Add a `Calendar` sidebar entry at `/calendar` without modifying Hermes core.
- Show month navigation and a day grid with activity counts.
- A day is active when at least one active Hermes message timestamp falls inside that date's `America/Chicago` midnight-to-midnight window.
- Clicking a day shows every matching session by actual title, owning profile, source, model, message count, tool-call count, and daily time range.
- For sessions crossing midnight, include the session on every day having messages but use only the selected day's messages for recap input.
- Show actual cron executions for the day, including job/run identity and status when available.
- Inventory must be read-only against Hermes session databases and cron stores.

### Manual summary session generation and optional daily roll-up

- Days and sessions do not receive automatic summaries.
- Every session card shows `Generate summary` when no current summary exists.
- Session generation uses the configured auxiliary compression profile via Hermes' built-in routing and never changes global model/delegation config.
- Store each session summary independently as structured JSON plus readable Markdown outside the plugin code directory.
- Key session artifacts by selected date plus the canonical composite identity `(profile, session_id)`. The model returns CONTENT-ONLY output (`summary` + `key_points`). Server-attached canonical identity is never trusted from model output and is always applied when saving.
- For sessions that cross midnight, summarize only messages inside the selected day's America/Chicago window.
- If one session exceeds the safe prompt budget, split only that session into lossless chunks, validate strict content-only output on every chunk, and reduce the chunk summaries into one result.
- Record selected date/window, profile/session identity and title, generation timestamp, collection cutoff, compression profile/model, and a deterministic per-session source fingerprint.
- If that session's selected-day activity changes after generation, mark only its summary stale and offer explicit regeneration.
- Never silently overwrite an existing summary. Regeneration must preserve an immutable timestamped prior version and require an explicit replace flag.
- Validate output before publishing it atomically.
- Offer an optional daily roll-up built only from saved session-summary artifacts and compact cron status metadata, never from the day's raw transcripts.
- A roll-up records exact included session identities, summary versions/fingerprints, and coverage (summarized sessions versus active sessions); partial coverage must be visible.
- A roll-up becomes stale when its included summary version/fingerprint changes or the active session set changes.
- Do not publish summaries or roll-ups to BookStack or Mnemosyne automatically.

## Safety and rollback

- Develop in this standalone repository, not in the Hermes Agent core checkout.
- Install under `~/.hermes/plugins/summarization-calendar` via a symlink or atomic copy.
- Persist ledger data under `~/.hermes/summarization-calendar`, never under the plugin installation directory.
- Include scripts or documented commands for install, status, uninstall/rollback, and reinstall.
- Installation must back up any pre-existing `summarization-calendar` plugin before replacement.
- Rollback must remove/restore plugin code without deleting ledger data.
- Plugin API inputs must validate dates, paths, limits, and profile names. SQL must be parameterized.
- Treat session text as untrusted data when prompting the summary model and when rendering HTML.

## Verification gates

1. Unit tests for timezone/DST boundaries, daily membership, multi-profile inventory, cross-midnight sessions, cron runs, fingerprinting, recap validation, atomic versioning, and rollback.
2. Frontend build/test passes.
3. Independent security/spec review passes.
4. Live Dashboard discovers the plugin and `/calendar` renders.
5. Live API inventories a known real day and matches an independent direct SQLite count.
6. A real summary session completes and its saved JSON/Markdown are read back and validated.
7. Adding a disposable source message or fixture changes only that session fingerprint and marks its summary stale without overwriting it.
8. A real optional daily roll-up is generated from saved summaries only, reports exact coverage, and contains no raw transcript input.
9. Rollback/uninstall is exercised, Dashboard returns to the prior plugin set, ledger data remains, and reinstall restores functionality.
10. Only after all gates pass: publish a public standalone repository with MIT license, README, screenshots, install/rollback instructions, security notes, and a release artifact/checksum.

## Scope exclusions for v1

- No automatic nightly recaps.
- No BookStack or Mnemosyne publishing.
- No recap editor.
- No weekly/monthly narrative synthesis.
- No Hermes core patch merely to add a Calendar glyph; use an existing supported icon.
- No destructive session or cron management actions.
