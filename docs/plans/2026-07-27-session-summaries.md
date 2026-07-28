# Per-Session Summary Model Summaries and Daily Roll-Up Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace raw whole-day recap generation with independently versioned per-session summary-model summaries and an optional daily roll-up generated only from saved summaries plus compact cron status metadata.

**Architecture:** Extend the existing read-only day inventory with deterministic per-session fingerprints, then add a separate session-artifact store and orchestration pipeline keyed by `(date, profile, session_id)`. Oversized sessions are split losslessly within one canonical identity and reduced to one summary. Daily roll-ups consume only validated stored summaries, record explicit coverage, and never read raw transcripts.

**Tech Stack:** Python 3.11 standard library, FastAPI/Pydantic plugin routes, shared 48 KiB fail-closed limit via `limits.MAX_MODEL_PROMPT_BYTES`, Hermes auxiliary compression via `agent.auxiliary_client.call_llm(task="compression", ...)`, immutable symlink-pointer storage, vanilla React plugin bundle, pytest, Node test runner.

---

## Fixed Contract

### Storage

Use hashed filesystem keys so untrusted profile/session strings never become path components:

- Current session summary pointer: `session-summaries/<date>/<artifact-key>`
- Immutable session versions: `session-versions/<date>/<artifact-key>/<version-id>/`
- Session job status: `running/sessions/<artifact-key>.json`
- Current roll-up pointer: `rollups/<date>`
- Immutable roll-up versions: `rollup-versions/<date>/<version-id>/`
- Roll-up job status: `running/rollups/<date>.json`

`artifact-key = sha256(date + NUL + profile + NUL + session_id)[:32]`.
Every immutable version contains `meta.json`, `raw.json`, and `summary.md`; metadata contains the canonical date/profile/session identity and title so the hash is inspectable without path decoding. Existing legacy `recaps/` and `versions/` data remains untouched and rollback/uninstall continues preserving the entire ledger root.

### API

All inputs are query parameters validated before discovery or filesystem access:

- `GET /session-summary?date=&profile=&session_id=`
- `POST /session-summary?date=&profile=&session_id=` body `{force_regenerate}`
- `GET /session-summary/versions?date=&profile=&session_id=`
- `POST /session-summary/rollback?date=&profile=&session_id=&version=`
- `GET /rollup?date=`
- `POST /rollup?date=` body `{force_regenerate}`
- `GET /rollup/versions?date=`
- `POST /rollup/rollback?date=&version=`

`GET /day` adds optional `source_fingerprint` and `summary_status` metadata to each session. No route returns raw transcript text or absolute filesystem paths. Existing `GET /recap` remains legacy read-only compatibility; raw whole-day generation is removed from the Calendar UI and `POST /recap` is not called by new code.

### Session generation

1. Discover the canonical `DailySession` for the requested date/profile/session ID.
2. Collect only that session's active messages inside the selected Chicago day window.
3. Recompute and compare its deterministic fingerprint before publishing.
4. Build one-session chunks using the existing lossless UTF-8 chunker.
5. Require every chunk result to contain exactly the canonical composite identity.
6. Canonicalize profile/session ID/title from inventory, never from compression output.
7. For multiple chunks, ask compression model to reduce only the segment summaries under the same identity; fall back to deterministic ordered concatenation only if reduction fails validation.
8. Validate non-empty summary, exact identity, source fingerprint, and collection cutoff.
9. Save immutable JSON/Markdown atomically, then swap the current pointer.

### Roll-up generation

1. Load current session summaries for the date; never call transcript collection.
2. Include only summaries whose stored canonical identity belongs to the current day inventory.
3. Pass compact summary text plus compact cron execution identity/status metadata to compression model via shared 48 KiB limit.
4. Record `active_session_count`, `included_session_count`, missing identities, included version IDs, included source fingerprints, and a deterministic roll-up input fingerprint.
5. Allow partial coverage but render it prominently (for example `5 of 7 sessions summarized`).
6. Mark stale when the active identity set changes, an included summary version/fingerprint changes, or a previously missing summary becomes available.

### Concurrency

- Session worker key: `session:<artifact-key>`
- Roll-up worker key: `rollup:<date>`
- Preserve the global maximum of four summary workers.
- Same-key requests conflict with HTTP 409.
- Durable job status survives page refresh and is recovered after Dashboard restart.

---

### Task 1: Add per-session fingerprints to the day contract

**Objective:** Produce deterministic per-session source fingerprints during the existing read-only inventory query without loading transcripts again.

**Files:**
- Modify: `dashboard/hermes_daily_ledger/contract.py`
- Modify: `dashboard/hermes_daily_ledger/inventory.py`
- Modify: `tests/test_inventory.py`
- Modify: `tests/test_api.py`

**Steps:**
1. Write failing tests proving two sessions receive distinct fingerprints and changing one message changes only that session's fingerprint.
2. Run targeted inventory tests and confirm RED.
3. Add optional `source_fingerprint` to `DailySession`.
4. Group existing `FingerprintComponent` rows by `(profile_label, session_id)` inside `query_day_sessions()` and call `compute_source_fingerprint()` per group.
5. Keep the current day-level fingerprint unchanged.
6. Run targeted tests and full inventory/API tests; confirm GREEN.
7. Commit: `feat: add per-session activity fingerprints`.

### Task 2: Build immutable session-summary and roll-up storage

**Objective:** Add failure-atomic, versioned artifact storage without modifying legacy recap data.

**Files:**
- Create: `dashboard/hermes_daily_ledger/session_storage.py`
- Create: `tests/test_session_storage.py`
- Reuse read-only helpers from: `dashboard/hermes_daily_ledger/recap_storage.py`

**Steps:**
1. Write failing tests for deterministic artifact keys, path traversal resistance, atomic first save, regeneration version preservation, pointer-swap failure rollback, concurrent version collision, load/list/rollback, permissions, and missing/corrupt metadata.
2. Run the new test file and confirm RED.
3. Implement generic immutable artifact save/load/list/rollback primitives for session and roll-up layouts, reusing only stable atomic helper behavior—not legacy path assumptions.
4. Store no raw transcript text.
5. Run new storage tests plus existing rollback/release-blocker tests; confirm GREEN.
6. Commit: `feat: add versioned session summary storage`.

### Task 3: Add keyed durable job status and concurrency

**Objective:** Track independent session and roll-up jobs with restart recovery and bounded global concurrency.

**Files:**
- Create: `dashboard/hermes_daily_ledger/summary_jobs.py`
- Create: `tests/test_summary_jobs.py`
- Modify: `dashboard/plugin_api.py`

**Steps:**
1. Write failing tests for same-session conflict, different-session coexistence, roll-up conflict, maximum four workers, failed/completed status persistence, and stale running recovery.
2. Run targeted tests and confirm RED.
3. Implement validated hashed job keys and atomic status files under the new running subdirectories.
4. Keep legacy recap status files readable but separate.
5. Run targeted and existing concurrency/API tests; confirm GREEN.
6. Commit: `feat: add keyed summary job control`.

### Task 4: Implement one-session compression orchestration

**Objective:** Generate, validate, reduce, and atomically save one canonical session summary.

**Files:**
- Create: `dashboard/hermes_daily_ledger/session_orchestrator.py`
- Modify: `dashboard/hermes_daily_ledger/transcript.py`
- Modify: `dashboard/hermes_daily_ledger/chunker.py`
- Reuse: `dashboard/hermes_daily_ledger/auxiliary_runner.py`
- Create: `tests/test_session_orchestrator.py`

**Steps:**
1. Write failing tests for exact composite identity lookup, cross-midnight selected-day filtering, no-activity rejection, single-chunk success, oversized one-session chunking, chunk identity drift rejection, canonical title restoration, validated reduction, deterministic fallback concatenation, fingerprint recheck before publish, and no raw transcript in artifacts.
2. Run targeted tests and confirm RED.
3. Add exact-session transcript collection that opens source SQLite in URI read-only mode.
4. Build prompts that frame transcript text as untrusted data and require exactly one identity.
5. Use `auxiliary_runner.run_auxiliary_compression()` with `call_llm(task="compression", ...)`; never place transcripts in argv or temp files.
6. Validate each chunk before reduction and final output before storage.
7. Run targeted tests, the auxiliary runner tests, and release blockers; confirm GREEN.
8. Commit: `feat: generate isolated compression session summaries`.

### Task 5: Implement summary-only roll-up orchestration

**Objective:** Generate an optional day narrative without reading raw transcripts.

**Files:**
- Create: `dashboard/hermes_daily_ledger/rollup_orchestrator.py`
- Create: `tests/test_rollup_orchestrator.py`

**Steps:**
1. Write failing tests proving the input contains saved summaries and compact cron metadata only, reports partial/full coverage, excludes foreign identities, records included versions/fingerprints, becomes stale on set/version changes, and preserves previous versions on regeneration.
2. Add a guard test that fails if transcript collection is called.
3. Run targeted tests and confirm RED.
4. Implement deterministic roll-up input fingerprint and compression prompt.
5. Validate non-empty narrative and exact coverage metadata before atomic save.
6. Run targeted and storage tests; confirm GREEN.
7. Commit: `feat: add summary-only daily rollups`.

### Task 6: Expose authenticated session-summary and roll-up APIs

**Objective:** Wire validated routes and background workers into the standalone Dashboard plugin.

**Files:**
- Modify: `dashboard/plugin_api.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_live_loader_contract.py`

**Steps:**
1. Write failing route tests for valid GET/POST, malformed dates/profiles/session IDs/version IDs, unknown session 404, existing summary without force 400, running conflict 409, worker failure status, no paths/transcripts in responses, and route presence under the real Hermes loader.
2. Run targeted tests and confirm RED.
3. Add route handlers and thread cleanup keyed by session/roll-up worker keys.
4. Enrich `/day` session dictionaries with current summary status using the already-computed fingerprint; do not load transcripts.
5. Keep authenticated plugin middleware behavior unchanged.
6. Run API/loader/full Python tests; confirm GREEN.
7. Commit: `feat: expose session summary and rollup APIs`.

### Task 7: Replace the whole-day recap UI

**Objective:** Add per-session controls and optional roll-up controls with durable polling/status recovery.

**Files:**
- Modify: `dashboard/dist/index.js`
- Modify: `dashboard/dist/style.css`
- Modify: `tests/test_frontend.js`

**Steps:**
1. Write failing frontend tests for a button on each session card, independent generating/error state by composite key, generated summary rendering, stale warning/regeneration, version restore, page-refresh recovery from queued/running status, bounded long polling, roll-up coverage display, disabled roll-up with zero summaries, and absence of the raw whole-day generation button.
2. Run Node tests and confirm RED.
3. Add per-session summary state keyed by date/profile/session ID; never share one global spinner.
4. Poll durable status and stop immediately on failed/completed state.
5. Add roll-up panel that clearly states `N of M sessions summarized` and lists missing titles when partial.
6. Preserve sanitization for all model-derived text.
7. Run syntax and frontend tests; confirm GREEN.
8. Commit: `feat: add per-session summary controls`.

### Task 8: Integration review and live verification

**Objective:** Prove the redesigned feature on real artifacts before calling it complete.

**Files:**
- Modify: `README.md`
- Modify: `PROJECT-BRIEF.md` only if implementation changed the fixed contract
- Add screenshots only after live success

**Steps:**
1. Run full Python, frontend, compile, shell syntax, and diff checks.
2. Dispatch spec-compliance review, then code-quality/security review; fix all critical/important findings and rerun reviews.
3. Reload only `hermes-dashboard.service`; do not restart the messaging gateway.
4. Verify `/calendar` inventories a known day and per-session status loads without transcript collection.
5. Generate one modest real session summary manually; read back JSON and Markdown, validate canonical identity/fingerprint/version, and confirm no source DB/cron content writes.
6. Exercise an oversized real session and verify one-identity chunk/reduction behavior.
7. Generate a daily roll-up and prove its compression input came only from saved summaries plus compact cron metadata.
8. Verify stale detection using a disposable fixture or injected test source, not by mutating live Hermes session databases.
9. Exercise rollback/uninstall/reinstall while preserving `~/.hermes/daily-ledger`.
10. Only after every gate passes, prepare publication artifacts.

---

## Abort and Revision Gates

- **Abort:** any source session DB or cron store content write attributable to the plugin.
- **Abort:** raw transcript text appears in stored metadata, API responses, process argv, status files, or roll-up prompts.
- **Abort:** model-supplied profile/session/title can replace canonical inventory identity.
- **Revision:** one session cannot complete within the configured 48 KiB context limit; lower the one-session chunk ceiling without changing external behavior.
- **Revision:** frontend requires one full transcript read per status badge; move status enrichment into `/day` using inventory fingerprints.
- **Revision:** roll-up hides partial coverage or reads raw transcripts.
