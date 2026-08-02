# Hermes Daily Ledger

A standalone Hermes Web Dashboard plugin that adds a Calendar page for browsing daily session and cron activity, generating manual per-session summaries, and optionally creating a daily roll-up from saved summaries.

![Hermes Calendar Plugin](./Hermes%20Calendar%20Plugin.png)

Daily Ledger does not patch Hermes core. It inventories Hermes session and cron stores read-only and writes its own versioned artifacts under the active `HERMES_HOME`.

## v1.1 behavior

- Calendar month navigation with activity counts.
- Daily session inventory across the default Hermes profile and named profiles.
- Session title with sanitized anchor that navigates to `/chat?resume=<session_id>&profile=<profile>`.
- Profile-scoped session links, restored model metadata, and browser-local auto-titled session visibility filter.
- Read-only cron execution inventory.
- Manual per-session summaries; nothing runs automatically.
- Lossless transcript chunking for larger sessions.
- Optional daily roll-ups built from current saved session summaries, not raw transcripts.
- Immutable JSON and Markdown versions with fingerprints, staleness detection, durable job state, and explicit regeneration.
- Summary routing through Hermes' configured `auxiliary.compression` task. The plugin contains no private provider or model fallback.
- Fixed `America/Chicago` day boundaries.

## Requirements

- Hermes Agent with the native Web Dashboard plugin SDK and backend plugin API loader.
- A Hermes build that provides `agent.auxiliary_client.call_llm` and a working `auxiliary.compression` route.
- Python 3.11 or newer in the Dashboard runtime.
- Linux for v1.0. Artifact locking uses POSIX `fcntl`; the supplied lifecycle scripts also use Bash and GNU coreutils.
- Node.js 20 or newer only for development tests; users install the prebuilt frontend bundle.

|The v1.1 release was validated on Hermes Agent v0.19.0 (2026.7.20), Python 3.11, and Node.js 23. Newer Hermes releases may change plugin contracts; see the compatibility notes in `SECURITY.md` and run the included validation before publishing or deploying an update.

## Install

Hermes discovers dashboard plugins at:

```text
$HERMES_HOME/plugins/daily-ledger/dashboard/manifest.json
```

For this standalone Dashboard extension, `dashboard/manifest.json` is the canonical package manifest. A root `plugin.yaml` or `plugin.json` is not required by the Dashboard plugin contract.

`HERMES_HOME` defaults to `~/.hermes`.

### From a release archive

Verify the downloaded archive first:

```bash
sha256sum -c SHA256SUMS
```

Extract it anywhere, then install an atomic copy:

```bash
tar -xzf hermes-daily-ledger-v1.0.0.tar.gz
cd hermes-daily-ledger-v1.0.0
./scripts/install-local.sh --copy
```

`--copy` is recommended for normal use. `--symlink` is available for development.

### From Git

Either clone directly into the Hermes plugin path:

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
git clone https://github.com/Ithrial/Hermes-Summarization-Calendar.git "${HERMES_HOME:-$HOME/.hermes}/plugins/daily-ledger"
```

Or clone elsewhere and run:

```bash
./scripts/install-local.sh --copy
```

### Load the backend

The frontend can be rediscovered by the Dashboard, but Python API routes are mounted only when the Dashboard process starts. Stop and relaunch `hermes dashboard` after installation or upgrade. If you manage the Dashboard with systemd, Docker, or another supervisor, restart the Dashboard process through that supervisor using its actual configured unit or service name.

Do not restart the Hermes messaging gateway for a Dashboard-only plugin change.

Then open the Dashboard and select `Calendar`. The plugin health route is:

```text
GET /api/plugins/daily-ledger/health
```

## Configure summarization

Daily inventory works without generating summaries. Manual summary and roll-up jobs use the active Hermes profile's standard compression-task configuration:

```text
auxiliary.compression.provider
auxiliary.compression.model
auxiliary.compression.base_url
auxiliary.compression.timeout
auxiliary.compression.reasoning_effort
```

Configure that route with Hermes itself. Daily Ledger never writes global model configuration and does not silently substitute a plugin-owned backend.

When a summary is requested, the selected day's transcript slice is sent to the configured compression model. Review `SECURITY.md` before using a remote provider.

## Data and privacy boundaries

Runtime code:

```text
$HERMES_HOME/plugins/daily-ledger/
```

Generated data:

```text
$HERMES_HOME/daily-ledger/
  session-versions/
  rollup-versions/
  running/
  .locks/
```

Source stores are opened read-only. Generated summaries are separate from plugin code and are preserved by default across install, upgrade, rollback, and uninstall.

The release archive never contains your Hermes configuration, credentials, sessions, cron database, logs, or generated Daily Ledger artifacts.

## Status, upgrade, rollback, and uninstall

Show installation and artifact counts:

```bash
./scripts/status-local.sh
```

Upgrade from a new checkout or extracted release:

```bash
./scripts/install-local.sh --copy
```

A pre-existing installation is snapshotted under:

```text
$HERMES_HOME/backups/daily-ledger-install/
```

List rollback IDs:

```bash
./scripts/rollback-local.sh
```

Restore one:

```bash
./scripts/rollback-local.sh <backup-id>
```

Uninstall code while preserving summaries:

```bash
./scripts/uninstall-local.sh
```

Removing generated data is intentionally separate and destructive:

```bash
./scripts/uninstall-local.sh --remove-data
```

Restart the Dashboard after upgrade, rollback, or uninstall so its Python router set matches the installed code.

## Development

The frontend is a prebuilt plain-JavaScript IIFE at `dashboard/dist/index.js`. It uses React and components supplied by `window.__HERMES_PLUGIN_SDK__`; React is not bundled.

Install development dependencies in a Python 3.11 environment:

```bash
python -m pip install -r requirements-dev.txt
./scripts/run-tests.sh
```

The test runner executes backend tests with outbound non-loopback sockets blocked, frontend Node tests, Python compilation, JavaScript syntax validation, Bash syntax validation, release validation, and diff checks.

Build release archives from a clean committed tree:

```bash
./scripts/build-release.sh --ref v1.0.0
```

See `CONTRIBUTING.md` and `docs/RELEASE.md`.

## Known v1.0 limits

- Day boundaries are fixed to `America/Chicago`.
- Daily roll-ups summarize saved session summaries rather than re-ingesting all raw transcripts.
- The runtime and lifecycle scripts are POSIX/Linux-oriented.
- Summary quality and runtime depend on the user's configured compression model.
- Dashboard backend plugin routes inherit Hermes' local-dashboard trust model; do not expose an untrusted Dashboard publicly.

## License

MIT. See `LICENSE`.
