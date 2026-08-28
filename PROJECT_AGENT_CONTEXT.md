# Project Agent Context

## Required context

- Purpose and audience: an invite-only, self-hosted Telegram media downloader for operators and the users they authorize.
- Wedge: send a media link or batch to Telegram and receive clean media without manually resolving, downloading, converting, and uploading it.
- General contract: supported URLs enter a durable provider-resolution and worker pipeline that selects a natural or explicit format, reports progress, and delivers Telegram media or files.
- Primary supported behavior: see `README.md`, `docs/behavior-inventory.md`, and the network-free journeys in `acceptance_tests/`.
- Important non-goals and limitations: see `README.md#current-boundaries`; there is no hosted service, web administration panel, access-control bypass, automated account recovery, or guaranteed provider availability.
- Sensitive data and privileged boundaries: bot and Telegram API credentials, PostgreSQL state, Redis queues, invitation access, provider cookies, Spotify credentials and cache, downloaded artifacts, production deployment, and persistent Docker volumes.

## Local verification

- Bootstrap: install uv, then run `git submodule update --init --recursive` and `uv sync --locked` with Python 3.14 available.
- Fast checks: `uv run prek run --all-files`.
- Full checks: `uv run pytest --cov=downloader_bot --cov-fail-under=80 --cov-report=term-missing`.
- Build and configuration: `docker compose -f docker-compose.example.yml config` and `docker compose build`.
- Integration or smoke tests: `scripts/docker_media_smoke.py`; this requires Docker, network access, and suitable provider inputs. Live Telegram and provider checks may require credentials and must remain separate from default tests.
- Documentation checks: `python3 .agent/oss-playbook/scripts/check_docs.py` validates the pinned playbook itself; project Markdown links and rendering require a separate review.
- Security checks: `scripts/security-check`; this requires Docker and registry/network access and runs Gitleaks plus Trivy.

## Publication and support

- Publication boundary: inspectable source, build and self-hosting instructions, safe configuration examples, and local verification. The repository does not promise a hosted instance, compatibility lifetime, contribution program, or response time.
- Supported versions: the latest `0.1.x` release and `main`; see `SECURITY.md` for the security-support boundary.
- Contribution and support policy: no formal policy is currently published.
- Contribution intake and maintainer-review policy: no formal intake or review SLA is currently published; acceptance remains at maintainer discretion.
- Agent-assisted public contribution policy: no project-specific policy is currently published.
- Private security reporting route: GitHub private vulnerability reporting as documented in `SECURITY.md`.
- External actions requiring maintainer authorization: pushes, tags, releases, repository settings, deployments, announcements, issue responses, and third-party submissions.

## Repository and release process

- Default branch: `main`; no public merge policy is currently documented.
- Generated and sensitive paths: see `.gitignore`, `.dockerignore`, and `README.md#security`.
- Version source: `pyproject.toml` and matching annotated semantic-version tags.
- Release workflow: run the documented local gates, verify the commit and CI, create an annotated tag matching the manifest version, then publish concise GitHub release notes. Never move a published tag.
- Artifact integrity policy: dependencies are locked in `uv.lock`; CI actions and security scanner images are pinned. No public runtime image or signed release artifact is promised.
- Deployment boundary and rollback: deployments are operator-owned external state. Preserve configuration and persistent volumes, back up state before destructive changes or migrations, and roll back to a verified source revision and compatible state.

## Enabled playbook profiles

- Enabled profiles: `none` — the pinned playbook has not published Python or Docker profiles yet.

## Project-specific decisions

- The playbook is a Git submodule because the maintainer prefers clean provenance and accepts recursive initialization in clones and CI.
- Application source is bind-mounted read-only by Compose; rebuild the image when the locked environment or media toolchain changes, and restart processes after source-only changes.
- The repository is primarily published for inspection and self-hosting; community process files should reflect actual maintainer capacity rather than imply a contribution program.

## Pinned playbook

- Upstream: `https://github.com/segfault-stack/oss-agent-playbook`
- Immutable ref: `v0.4.4`
- Resolved commit: `ef9281d1f1ff38fdb99a5b5b2d1248c088b4c1fa`
- Import method: `git submodule`
- Imported or last reviewed: `2026-08-26`
