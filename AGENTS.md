# Agent instructions

The pinned OSS Agent Playbook is located at `.agent/oss-playbook`.

Before publication, repository maintenance, CI, release, security, or community work:

1. Read `.agent/oss-playbook/docs/principles.md` and `.agent/oss-playbook/docs/agent-workflow.md`.
2. Read `PROJECT_AGENT_CONTEXT.md` for this project's facts, commands, risks, and enabled profiles.
3. Use `.agent/oss-playbook/README.md` to select only the task-specific documents that apply.

If the pinned playbook is missing, stop and report the checkout problem. Do not fetch a mutable replacement automatically.

Platform and system safety constraints remain authoritative. The current request defines task scope; repository and directory instructions specialize work in their scope. Local file changes do not imply authorization to push, release, change remote settings, deploy, or communicate externally.

## Project structure

- Keep framework-free models in `downloader_bot/domain`.
- Put use cases and ports in `downloader_bot/application`.
- Keep Telegram and provider adapters in `downloader_bot/adapters`.
- Put PostgreSQL, Redis, downloader, and migration implementations in `downloader_bot/infrastructure`.
- Keep process assembly in `downloader_bot/bootstrap`.
- Add network-free behavior tests under `acceptance_tests/` and operational checks under `scripts/`.

Keep Telegram routers transport-only and move reusable behavior into application use cases. Preserve the domain/application dependency boundary.

## Local workflow

Use the locked uv environment and the repository's existing tools:

```bash
uv sync --locked
uv run prek run --all-files
uv run pytest --cov=downloader_bot --cov-fail-under=80 --cov-report=term-missing
docker compose -f docker-compose.example.yml config
scripts/security-check
```

Use four-space indentation, type hints at public boundaries, small async functions for I/O, and standard Python naming. Ruff enforces style and ty performs static type checking. Mock Telegram, providers, PostgreSQL, and Redis in the default test suite; keep live service checks separate.

## Security and state

Never commit `.env`, bot tokens, API credentials, cookie jars, Spotify sessions, database dumps, downloaded media, or logs. Treat exported cookies and cached provider sessions as credentials. Do not delete or replace PostgreSQL, Redis, Telegram Bot API, download, or Spotify volumes without explicit authorization and a verified target.

The playbook and librespot are pinned Git submodules. Initialize them with:

```bash
git submodule update --init --recursive
```
