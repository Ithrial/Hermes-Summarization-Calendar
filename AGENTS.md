# Worker Instructions

- Read `PROJECT-BRIEF.md` before making changes.
- This is a standalone Hermes Web Dashboard plugin; do not modify the Hermes Agent core checkout.
- Treat the Hermes Agent installation as a read-only runtime dependency and reference source.
- Follow TDD for deterministic Python logic and frontend helpers.
- Never read or print secrets from `~/.hermes/.env` or profile `.env` files.
- Never mutate live Hermes session databases or cron job stores.
- Keep generated user data outside plugin code under a configurable ledger root.
- Use standard-library Python where practical; the dashboard runtime already provides FastAPI/Pydantic.
- Do not install or activate a live plugin from an automated worker. The maintainer owns live installation, service restart, rollback, and publication.
- Keep completion summaries concise: files changed, tests run with exact results, blockers, and commit SHA.
