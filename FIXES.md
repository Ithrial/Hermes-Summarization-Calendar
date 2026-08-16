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

## What was NOT changed (needs Sean's decision — not started)

- **QA finding 3 (Important)** — legacy `POST /recap` still generates
  raw-transcript recaps, bypassing the summary-only roll-up boundary. QA's
  recommendation is read-only migration access or a revised spec contract.
  Decision needed: gate it read-only, or amend the brief.
- **QA finding 4 (spec deviation)** — session cards lack the brief-required
  card-level `Generate summary`; current tests assert the control is absent.
  Decision needed: restore the control, or approve batch-only UX and update
  the brief.
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
| New regressions in `tests/` | 6 passed (5-segment near-limit reduction, pre-flight byte rejection, call budget, recap/batch worker-start hygiene, PID-free job_id) |
| Pinned suites (orchestrator, identity, batch, recap, API, packaging) | all green — small-session single-pass reduction, `segment_count`, fallback ordering, and version contracts preserved |
| Full release gate (`scripts/run-tests.sh`, Linux, network blocked) | **passed** — backend 478 passed, frontend 172 passed, static validation + release validation for v1.2.4 |
| Reproduction run of the original QA commands | `PYTHONPATH="tests:dashboard:scripts" python -m pytest -q -p conftest reproductions/test_qa_findings.py` → 2 passed |

Live gates 4–9 (live Dashboard render, real model call, live SQLite count,
mutation/staleness, real roll-up, rollback) remain operator-blocked as in the
QA report; nothing in this branch changes what those gates verify.
