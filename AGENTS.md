# Repository Guidelines

## Project Structure & Architecture

Application code lives in `downloader_bot/`. Keep framework-free models in `domain/`, use cases and ports in `application/`, Telegram/provider integrations in `adapters/`, PostgreSQL, Redis, downloads, and migrations in `infrastructure/`, and process assembly in `bootstrap/`. Telegram routers should remain transport-only; reusable behavior belongs in application use cases. Network-free behavior tests live in `acceptance_tests/`, operational checks in `scripts/`, and design notes and images in `docs/`. `third_party/librespot` and `.agent/oss-playbook` are pinned submodules.

## Build, Test, and Development Commands

Use Python 3.14 and the locked `uv` environment:

```bash
git submodule update --init --recursive
uv sync --locked
uv run prek run --all-files
uv run pytest --cov=downloader_bot --cov-fail-under=80 --cov-report=term-missing
docker compose -f docker-compose.example.yml config
```

`prek` runs the repository quality hooks, including Ruff and ty. Pytest runs the default network-free suite and enforces 80% production-code coverage. The Compose command validates deployment configuration. `docker compose build` builds runtime images; `scripts/security-check` runs containerized Gitleaks and Trivy checks and requires Docker/network access.

## Coding Style & Naming Conventions

Use four-space indentation, standard Python naming (`snake_case` functions/modules, `PascalCase` classes), and type hints at public boundaries. Prefer small async functions for I/O. Preserve the domain/application dependency boundary and use typed ports for infrastructure interactions. Ruff handles linting and formatting; ty performs static analysis.

## Testing Guidelines

Name tests `test_*.py` and test functions `test_*`. Add behavior-focused coverage under `acceptance_tests/`. Mock Telegram, providers, PostgreSQL, and Redis in the default suite; keep credentialed or live-provider smoke checks separate. Run the full coverage command before submitting changes.

## Commits & Pull Requests

Recent history uses Conventional Commit prefixes such as `feat:`, `fix:`, `perf:`, `docs:`, and `chore:`. Keep commits focused and write imperative, descriptive subjects. Pull requests should explain the behavior change, risks, and verification performed; link relevant issues and include screenshots only for visible UI/documentation changes. Never claim checks passed unless they were run.

## Security & Agent Guidance

Never commit `.env`, tokens, cookies, provider sessions, dumps, downloaded media, or logs. Do not replace persistent PostgreSQL, Redis, Telegram API, download, or Spotify volumes without explicit approval. Before repository maintenance, CI, release, security, or community work, read `PROJECT_AGENT_CONTEXT.md`, `.agent/oss-playbook/docs/principles.md`, and `.agent/oss-playbook/docs/agent-workflow.md`. If the pinned playbook is missing, stop instead of fetching a replacement. Local edits do not authorize pushes, releases, deployments, or external messages.
