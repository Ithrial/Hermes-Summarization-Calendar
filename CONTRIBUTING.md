# Contributing

Hermes Summarization Calendar is a standalone Web Dashboard plugin. Keep changes inside this repository; Hermes Agent core is a read-only dependency and must not be patched to make this plugin work.

## Development setup

Requirements:

- Python 3.11 or newer;
- Node.js 20 or newer;
- Bash and GNU coreutils on Linux.

Install test dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run the complete local gate:

```bash
./scripts/run-tests.sh
```

## Change discipline

- Preserve read-only access to Hermes session and cron stores.
- Keep generated data outside plugin code.
- Do not hard-code a provider, model, profile, endpoint, credential, or private hostname.
- Route summarization through Hermes' `auxiliary.compression` task.
- Treat session titles, transcript text, cron metadata, and model output as untrusted data.
- Add a failing regression test before changing deterministic backend or frontend behavior.
- Do not weaken identity, fingerprint, schema, staleness, or atomic-publication validation.
- Backend route changes require an exact-loader regression and a Dashboard process restart during live acceptance.
- Never allow unit tests to launch a real model worker or make an outbound model request.

## Frontend

`dashboard/dist/index.js` is intentionally a prebuilt plain-JavaScript IIFE using `window.__HERMES_PLUGIN_SDK__`. React and Dashboard UI components are external and supplied by Hermes. Node's built-in test runner exercises the bundle; no npm dependency installation is required.

## Pull request gate

Before submitting:

```bash
./scripts/run-tests.sh
python scripts/validate-release.py
```

Document user-visible behavior in `CHANGELOG.md`. Do not commit generated files under `artifacts/`.

## Commit style

Use concise conventional subjects such as:

```text
fix: preserve summary version during failed regeneration
feat: add configurable calendar timezone
chore: prepare v1.0.1 release
```
