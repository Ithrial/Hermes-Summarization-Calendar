# Security Policy

## Supported versions

Security fixes are applied to the current `1.0.x` line until a newer stable line replaces it.

## Trust boundary

Daily Ledger is a local Hermes Web Dashboard plugin. It reads Hermes session and cron stores and can send selected transcript text to the user's configured `auxiliary.compression` model when the user manually requests a summary.

Hermes Dashboard backend plugin routes use the Dashboard's local trust model and may not require a separate session token. Do not bind or publish a Dashboard containing untrusted plugins to an untrusted network. Prefer localhost, a trusted LAN, or an authenticated reverse proxy with appropriate access controls.

## Data handling

- Hermes session databases and cron stores are opened read-only.
- Daily Ledger writes only beneath `$HERMES_HOME/daily-ledger` unless `LEDGER_ROOT` is explicitly set.
- Summary artifacts can contain sensitive information derived from conversations and tool activity. Protect the Hermes home directory and backups accordingly.
- Manual summary generation sends the selected transcript slice to the configured compression provider. A remote provider receives that content under its own privacy terms.
- The plugin does not require or ship API keys. Provider credentials remain managed by Hermes.
- Error messages and API responses are sanitized to avoid exposing filesystem paths, process IDs, and credential-like values.

## Release hygiene

Official release archives are generated from a clean Git tree by `scripts/build-release.sh` and accompanied by `SHA256SUMS` plus `release-manifest.json`. Verify checksums before installation.

Release archives must not contain:

- `.env` files, auth stores, or credentials;
- Hermes `state.db`, cron databases, or logs;
- `$HERMES_HOME/daily-ledger` generated artifacts;
- model-server captures or provider configuration;
- local backup directories.

## Reporting a vulnerability

Do not open a public issue containing private session data, credentials, filesystem paths, or exploit details. Contact the repository maintainer privately through the security-reporting method configured on the repository. Include:

- affected Daily Ledger version;
- Hermes Agent version;
- reproduction steps using synthetic data where possible;
- expected and observed behavior;
- impact and any known workaround.

## Scope notes

Native Windows is not supported in v1.0 because artifact locking uses POSIX `fcntl`. The included lifecycle scripts require Bash and GNU coreutils. These are compatibility limits, not security guarantees.
