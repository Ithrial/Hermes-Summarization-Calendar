# Security Policy

## Supported versions

Security fixes target the current `1.2.x` stable line. Older release lines are
not guaranteed to receive security fixes.

## Trust boundary

Summarization Calendar is a local Hermes Web Dashboard plugin. It reads Hermes session and cron stores and can send selected transcript text to the user's configured `auxiliary.compression` model when the user manually requests a summary.

Hermes Dashboard backend plugin routes use the Dashboard's local trust model and may not require a separate session token. Do not bind or publish a Dashboard containing untrusted plugins to an untrusted network. Prefer localhost, a trusted LAN, or an authenticated reverse proxy with appropriate access controls.

## Data handling

- Hermes session databases and cron stores are opened read-only.
- Summarization Calendar writes only beneath `$HERMES_HOME/summarization-calendar` unless `LEDGER_ROOT` is explicitly set.
- Summary artifacts can contain sensitive information derived from conversations and tool activity. Protect the Hermes home directory and backups accordingly.
- Manual summary generation sends the selected transcript slice to the configured compression provider. A remote provider receives that content under its own privacy terms.
- The plugin does not require or ship API keys. Provider credentials remain managed by Hermes.
- Known filesystem paths, process IDs, and credential-like values are removed
  from normal health, job-status, generation-error, and batch-creation error
  responses. API routes must not return raw internal exception text.

## Release hygiene

Official release archives are generated from a clean Git tree by `scripts/build-release.sh` and accompanied by `SHA256SUMS` plus `release-manifest.json`. Verify checksums before installation.

Release archives must not contain:

- `.env` files, auth stores, or credentials;
- Hermes `state.db`, cron databases, or logs;
- `$HERMES_HOME/summarization-calendar` generated artifacts;
- model-server captures or provider configuration;
- local backup directories.

## Reporting a vulnerability

Do not open a public issue containing private session data, credentials,
filesystem paths, or exploit details. Use GitHub's private vulnerability
reporting form:

<https://github.com/Ithrial/Hermes-Summarization-Calendar/security/advisories/new>

Include:

- affected Summarization Calendar version;
- Hermes Agent version;
- reproduction steps using synthetic data where possible;
- expected and observed behavior;
- impact and any known workaround.

## Scope notes

Native Windows is not supported in the `1.2.x` release line because artifact
locking uses POSIX `fcntl`. The included lifecycle scripts also require Bash
and GNU coreutils. These are compatibility limits, not security guarantees.
