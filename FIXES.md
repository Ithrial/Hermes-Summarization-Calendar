# v1.2.4 Fixes: QA Findings + Security Scan Remediation

Branch: `fix/v1.2.4` (from `e744c24` / v1.2.3)
Inputs: `hermes-calendar-qa/QA-REPORT.md` (3 Important + 1 spec deviation) and
`hermes-calendar-qa/security-scan-results/report.md` (2 Medium, 4 Low) at the
same revision.

## What changed

### QA finding 1 (Important) — oversized sessions failed during reduction

**Problem.** Every individually validated chunk summary can be up to 12,000
characters. The old design combined *all* chunk summaries into a single
reduction prompt checked against the same 48 KiB per-prompt ceiling, so five
or more near-limit summaries (~60 KiB) always failed the job with
`Reduction prompt exceeds size limit` — after every chunk call had already
spent provider quota.

**Fix.** Hierarchical reduction in
`dashboard/hermes_summarization_calendar/session_orchestrator.py`:

- `_pack_reduction_groups()` greedily packs segment summaries into groups
  whose reduction prompt fits under the ceiling (each group's prompt size is
  measured, not estimated).
- `generate_session_summary()` reduces level by level: when all summaries
  fit one prompt, that is the final reduction (identical to the old path for
  small sessions — pinned tests prove it); otherwise each prompt-sized group
  is reduced now and the group-level summaries are reduced next level, until
  one summary remains. Every level strictly shrinks the list, so the loop
  terminates; a liveness guard fails the job with a stable error if a level
  cannot shrink (pathologically large summaries where no two fit one prompt).
- Fallback semantics preserved: if any reduction call fails validation, the
  summary falls back to the ordered, validated segments at that level
  (`validated-segment-fallback`), and `segment_count` still counts the
  original chunks.

### QA finding 2 (Important) — worker-start errors disclosed internals

**Problem.** `POST /session-summary/batch` and legacy `POST /recap`
interpolated the raw `worker.start()` exception into the HTTP 500 response.
A thread-start failure can carry filesystem paths, process metadata, or
credential-like values; the QA repro confirmed both a macOS path and a
token-like value reached the public response.

**Fix.** `dashboard/plugin_api.py`:

- Both worker-start failure paths now return fixed, pre-approved public
  messages (no exception text), and log the full exception server-side via
  `logger.exception`.
- Removed the now-orphaned `import os` (its last use was the PID below).

### Scan finding 1 (Medium) — unbounded provider calls per job (CWE-770)

**Problem.** Chunking bounds each prompt, but nothing bounded the aggregate
work of one user-triggered job: total transcript bytes, chunk count, or
provider calls.

**Fix.** Cumulative per-job budgets in
`dashboard/hermes_summarization_calendar/limits.py` +
`session_orchestrator.py`:

- `MAX_SESSION_SOURCE_BYTES = 2 MiB` — total raw transcript bytes. Checked
  **before the first provider call**; oversized sessions are rejected with a
  stable retryable error and spend zero provider quota.
- `MAX_SESSION_PROVIDER_CALLS = 64` — cumulative chunk + reduction calls.
  Checked before each call, so an overlong job stops at the budget instead
  of running to completion. (At worst-case chunking the byte budget fires
  first; the call budget is the backstop.)
- Both are keyword parameters on `generate_session_summary()` with safe
  defaults; the batch path inherits them because batch members run through
  the same function.

### Scan finding 3 (Low) — recap job_id disclosed the PID (CWE-200)

**Fix.** Recap queue responses now return `recap-<date>-<16 hex chars>` from
`secrets.token_hex(8)` instead of `os.getpid()`. The date-scoped status
endpoint already resolves jobs; the identifier is opaque and unique.

### Scan finding 5 (Low) — recap status files relied on process umask (CWE-276)

**Fix.** `dashboard/hermes_summarization_calendar/concurrency.py`:

- The `running/` status directory is created `0700` and re-`chmod`ed for
  directories created under older versions with a permissive umask.
- Status JSON files (and their atomic-write temp files) are written
  owner-only `0600` via `os.open(..., 0o600)`, so confidentiality no longer
  depends on the process umask or parent-directory permissions.

### Scan finding 6 (Low) — API-visible failures retained paths/credentials

**Fix.** New `dashboard/hermes_summarization_calendar/error_policy.py` is the
single boundary redactor for error text that crosses the HTTP boundary:

- Patterns: bearer/authorization values, keyed credentials (`token=...`,
  `api_key: "..."`), `token_xxx`-style identifiers, credential-bearing URLs
  (`scheme://user:pass@host`), long hex and long opaque strings, PIDs, and
  absolute Unix paths including macOS `/Users` and `/private`.
- `concurrency._sanitize_error()` (used by `summary_jobs` for session and
  rollup job state) now delegates to it, so every persisted, API-visible job
  error is stored redacted.

**Deliberately NOT converged** (documented in `error_policy.py`):

- `inventory._sanitize_error()` feeds the deterministic **cron fingerprint
  digest**; changing its behavior would change fingerprints and mark every
  existing summary stale. It stays.
- `batch_orchestrator`, `batch_jobs`, and `auxiliary_runner` sanitizers carry
  pinned behavioral tests with their own placeholder contracts and serve
  storage/log surfaces, not the public response boundary. They stay.

## QA finding 3 (Important) — legacy `POST /recap` raw-transcript generation

**Decision (Sean, 2026-08-16):** retire generation, keep read/management access.

**Problem.** The route was callable by any Dashboard-authorized API caller and
collected whole-day raw transcripts for model processing, bypassing the
summary-only roll-up boundary in the project brief. The active frontend does
not call it (the frontend suite even pins that the bundle never calls the
retired raw whole-day recap API).

**Fix.** `dashboard/plugin_api.py`:

- `POST /recap` now returns a stable **410** (`recap_generation_retired`)
  with a fixed message pointing callers at `POST /session-summary/batch`
  and `POST /rollup`. It has no generation side effects: no slot, no worker,
  no job, no model call. The request signature (date query + optional
  `force_regenerate` body) is kept stable so pre-v1.2.4 clients get a clear
  retirement response instead of a validation error; date validation still
  runs first (400 semantics preserved).
- The legacy worker thread (`_run_recap_worker`) and its import of
  `generate_recap` were removed from the API module. `generate_recap` remains
  in `recap_orchestrator` (its pinned signature contract is untouched) but is
  no longer reachable from the HTTP boundary.
- Preserved unchanged: `GET /recap` (status/staleness), `GET /recap/versions`,
  `POST /recap/rollback`, month-grid `has_recap`/`recap_stale` flags, and
  startup stale-lock recovery. Existing stored recaps are never touched.

`PROJECT-BRIEF.md` was amended to record both decisions: the batch-toolbar
UX (checkboxes + shared toolbar) is the approved generation interface —
superseding the v1.0 per-card `Generate summary` button, which was removed
as clunky, with the selection/batch scaffolding kept in preparation for
aggregate multi-session summarization — and legacy raw recap generation is
retired with read/management access preserved.

**Test contract changes:** the generation pins (202-queued, per-date 409,
`recap_already_exists` 400, recap worker-start 500, recap `job_id` PID
regression) were replaced by retirement regressions (410 fixed message,
no side effects, stored recap untouched, read access survives). The E12
pending-worker capacity regression moved from the retired recap path to the
batch route; the E11 pool-cleanup static pin now targets the surviving
`pool_key`-keyed routes.

## What was NOT changed (deferred — no exposure, documented)

- **QA finding 4 (spec deviation)** — resolved by brief amendment, not code:
  the batch-only UX is the approved design (see above).
- **Scan finding 2 (Medium)** — month inventory re-reads the full cron
  executions table per day (bounded single range query is the fix). Deferred:
  perf hardening, larger blast radius on the month endpoint, no data
  exposure.
- **Scan finding 4 (Low)** — provider responses parsed without a local byte
  bound. Deferred: requires control of the configured provider; transport
  caps apply in practice.

## Verification

| Check | Result |
|---|---|
| Inverted QA defect repros (`reproductions/test_qa_findings.py`) | 2 passed — both original defects now assert FIXED behavior |
| New regressions in `tests/` | 6 passed (five near-limit segment summaries, pre-flight byte rejection, call budget, worker-start message hygiene, opaque batch job ID) plus 8 legacy-recap retirement regressions (410 contract, no side effects, stored recap untouched, read access preserved, capacity on batch) |
| Pinned suites (orchestrator, identity, batch, recap, API, packaging, regression, release-blockers, live-loader) | all green — small-session single-pass reduction, `segment_count`, fallback ordering, version contracts, and the `/recap` loader contract preserved |
| Full release gate (`scripts/run-tests.sh`, Linux, network blocked) | **passed** — backend 483 passed, frontend 172 passed, static validation + release validation for v1.2.4 |
| Reproduction run of the original QA commands | `PYTHONPATH="tests:dashboard:scripts" python -m pytest -q -p conftest reproductions/test_qa_findings.py` → 2 passed |

Live gates 4–9 (live Dashboard render, real model call, live SQLite count,
mutation/staleness, real roll-up, rollback) remain operator-blocked as in the
QA report; nothing in this branch changes what those gates verify.
