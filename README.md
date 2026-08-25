<div align="center">

# Media Pocket

<img src="docs/media-pocket-hero.png" alt="Media Pocket — Send a link. Get clean media back." width="100%">

**An invite-only, self-hosted Telegram downloader for video, audio, social posts, albums, playlists, and batches.**

**Status:** early `0.1.x` self-hosted releases; provider compatibility can change with upstream services.

[![CI](https://github.com/segfault-stack/media-pocket/actions/workflows/ci.yml/badge.svg)](https://github.com/segfault-stack/media-pocket/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/segfault-stack/media-pocket)](LICENCE)
![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

[Features](#what-it-does) ·
[Sources](#supported-sources) ·
[Deployment](#deployment) ·
[Usage](#using-the-bot) ·
[Security](#security)

</div>

## ✨ What it does

Media Pocket turns Telegram into a private media inbox. Send one link or a batch; the bot resolves the provider, chooses the natural format, shows live progress, and returns the finished media.

- **Natural by default.** Video stays video, audio-first sources stay audio, and YouTube follows each user's saved preference.
- **Clean in Telegram.** Media carries no caption or source URL, non-audio filenames are randomized, and actions arrive separately.
- **Built for collections.** Single links, batches, albums, and playlists share the same durable worker pipeline.
- **Private at every entry point.** Invitations protect chats, groups, Business messages, callbacks, and inline mode.

The bot and workers run as separate processes. PostgreSQL stores canonical state, Redis Streams carries jobs and progress, and shared artifact storage connects downloading to Telegram delivery.

> [!WARNING]
> Media Pocket does not bypass provider access controls. Private, deleted, region-restricted, or authentication-gated content may require valid cookies or may remain unavailable. Operators are responsible for complying with provider rules and local law.

## 🌍 Supported sources

| Category | Sources | Available handling |
| --- | --- | --- |
| Video | YouTube | video, audio, playlists, quality selection, per-user default |
| Social | TikTok, Instagram, X / Twitter, Threads, Pinterest | media-first delivery, audio extraction, files, provider collections |
| Audio | Spotify, SoundCloud, Zaycev.net, HitMoz | tracks, albums and playlists where supported, native Spotify audio or fallback |
| Generic | HTTP(S) links | format detection and yt-dlp resolution where supported |

Video sources can offer **Video** or **Audio**, **Best / 1080p / 720p / 480p**, and **Media** or **File** delivery where those choices apply. Social posts do not show a fake resolution selector. Audio-first providers expose only relevant controls.

Video is normalized to streaming-compatible MP4 with H.264/AAC. Audio is normalized to M4A with AAC-LC and includes title, performer, duration, and thumbnail metadata when the provider supplies it.

## 🔄 Download flow

1. **Resolve** — identify the provider and apply its natural mode or the user's YouTube preference.
2. **Queue** — persist a job snapshot and atomically enqueue it for a worker.
3. **Process** — update one status message with queue position, progress, speed, size, and ETA when available.
4. **Deliver** — send only the file or media group, remove the technical status after success, and keep optional actions in a separate message.

Ordinary links start immediately. YouTube can be configured per user as **Video**, **Audio**, or **Always ask**; only **Always ask** opens the 15-minute format card. A YouTube URL containing a playlist always asks whether to download the current video or the entire playlist before anything is queued. Explicit `/audio URL`, `/video URL`, `!a`, `!audio`, `!mp3`, and `!music` still override the saved format, but they do not bypass the playlist-scope choice.

For batches, each link keeps its provider-native mode. If a batch contains YouTube and **Always ask** is enabled, one choice applies to the batch, with the nearest valid fallback for incompatible items.

Failures remain visible as a compact card with a human-readable reason, a next step, retry, format change, and help actions. Tracebacks, provider internals, and internal error codes are never shown to users.

## 🚀 Deployment

### Requirements

- a Linux host with Docker Engine and the Docker Compose plugin;
- Git;
- a Telegram bot token from [@BotFather](https://t.me/BotFather);
- Telegram API credentials from [my.telegram.org](https://my.telegram.org/);
- enough CPU, memory, storage, and network capacity for FFmpeg and the intended worker concurrency.

Spotify Premium and provider cookies are optional and only needed for the corresponding provider paths.

### 1. Prepare Telegram

Create the bot with [@BotFather](https://t.me/BotFather) and keep its token private.

For inline downloads, run `/setinline`, then `/setinlinefeedback` and choose **Enabled**. Media Pocket confirms an inline download from Telegram's chosen-result update, so sampled feedback modes such as `1/10` or `1/100` are not suitable.

If the bot should receive ordinary links in groups without being mentioned, adjust its group privacy setting in BotFather. The bundled local Telegram Bot API service requires `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

### 2. Configure the host

```bash
git clone --recurse-submodules --branch v0.1.0 \
  https://github.com/segfault-stack/media-pocket.git
cd media-pocket

cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
```

Set the four required values in `.env`:

```env
BOT_TOKEN=replace-with-bot-token
POSTGRES_PASSWORD=replace-with-a-long-random-password
TELEGRAM_API_ID=replace-with-api-id
TELEGRAM_API_HASH=replace-with-api-hash
```

### 3. Build and initialize

```bash
docker compose build
docker compose up -d postgres redis telegram-bot-api
docker compose run --rm migrate
```

The bot refuses to start until PostgreSQL contains at least one administrator:

```bash
docker compose run --rm migrate \
  python -m downloader_bot admin add YOUR_TELEGRAM_USER_ID
```

Start the bot and one or more workers:

```bash
docker compose up -d --scale worker=2
docker compose ps
```

The image contains the locked Python environment and media toolchain. Compose mounts the checkout at `/app` read-only and provides separate writable volumes for downloads, logs, cookies, and Spotify state. Restart the bot and workers after source changes; rebuild only when dependencies or the toolchain change.

## 🤖 Using the bot

Open the bot, run `/admin`, create an invitation code, and redeem it from each account that should have access. Then send a supported link or several links in one message.

| Command | Purpose |
| --- | --- |
| `/start` | onboarding, inline shortcut, settings, help, group installation, and sharing |
| `/help` | platforms, formats, batches, inline mode, and limitations |
| `/settings` | YouTube default, quality, delivery, result actions, cleanup, and progress detail |
| `/status` | the current user's active and recent jobs, cancellation, and retry |
| `/audio URL` | download immediately as audio |
| `/video URL` | download immediately as video |
| `/redeem CODE` | unlock access with an invitation |
| `/admin` | create, list, share, and revoke invitations |
| `@your_bot URL` | submit inline from any chat after access is granted |

Single results can expose contextual actions such as **Get audio**, **Get video**, **Send as file**, **Source**, and **Share**. Albums are delivered as media groups followed by one compact action card.

## 🔐 Invite-only access

At least one administrator must be created from the command line before the bot starts. Administrators can generate:

- **single-use invites** — one redemption, no expiry;
- **limited-use invites** — any configured limit from 2 to 100,000 redemptions, no expiry;
- **timed invites** — multiple redemptions until the configured expiry.

Users send the bare code or `/redeem CODE`. A successful redemption grants access until the database record is changed by the operator.

Inline mode is not a side door: unauthorized users only receive a prompt to open the private chat and enter a code. Callback ownership is checked as well, so another user cannot confirm, cancel, or retry someone else's request.

## 🔌 Provider setup

<details>
<summary><strong>YouTube compatibility stack</strong></summary>

The image pins yt-dlp with its default extras, the recommended `curl-cffi` browser-impersonation transport, yt-dlp EJS challenge scripts, Deno, and FFmpeg/ffprobe.

Compose starts the matching Deno-based BgUtils PO-token provider. The Python plugin connects yt-dlp to that private sidecar, and YouTube extraction uses the `mweb` client so GVS PO tokens are requested automatically. `YOUTUBE_POT_PROVIDER_URL` can override the internal endpoint.

This is compatibility plumbing, not an access-control bypass. Cookies may still be required for account-gated material, rate limits still apply, and YouTube can change extraction requirements without notice.

</details>

<details>
<summary><strong>Spotify Premium audio</strong></summary>

The image builds a pinned librespot-based `spotify-streamer` helper from the included Git submodule. With a deployment-owned Spotify Premium session, tracks, albums, and playlists use native Spotify audio first. If authentication is missing or an individual native stream fails, Media Pocket falls back to its YouTube search path.

Authenticate without placing a browser on the server:

```bash
SPOTIFY_SSH_HOST=example.com SPOTIFY_SSH_PORT=2222 scripts/spotify auth
```

Run the printed SSH tunnel command on your computer, keep it open, and visit the displayed Spotify URL locally. The callback remains bound to the server's `127.0.0.1`.

Useful operator commands:

```bash
scripts/spotify check
scripts/spotify resolve https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6
scripts/spotify reset-auth
```

Spotify credentials are stored in PostgreSQL. The librespot cache lives in the `spotify_cache` volume. `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are optional and support collection expansion for the fallback path; they are not Premium account credentials.

</details>

<details>
<summary><strong>Provider cookies and Media Cookie Broker</strong></summary>

yt-dlp can read a combined Netscape cookie jar or provider-specific files from `cookies/`. Treat every cookie file as a password and never commit it.

The optional `cookie-broker` Compose profile can run a separately configured `cookie-sync` image and write refreshed cookie jars into the shared directory:

Configure `COOKIE_BROKER_IMAGE`, `BROKER_URL`, `BROKER_USERNAME`, and `secrets/cookie-broker-password` first, then run:

```bash
sudo chown -R 1000:1000 cookies secrets/cookie-broker-password
sudo chmod 700 cookies
sudo chmod 600 secrets/cookie-broker-password
docker compose --profile cookie-broker up -d cookie-sync
```

The bot, workers, and cookie-sync share UID/GID `1000:1000` for these deployment-owned files. Keep the secret readable only by that owner.

[Media Cookie Broker](https://github.com/segfault-stack/media-cookie-broker) is a related but separately deployed project. Media Pocket does not bundle it; its image, endpoint, credentials, and network policy remain operator choices.

</details>

## 🛡️ Security

Media Pocket handles bot tokens, provider sessions, browser cookies, and user-submitted URLs.

- Never commit `.env`, `secrets/`, cookie jars, Spotify sessions, database dumps, or downloaded media.
- Use a long random PostgreSQL password.
- Keep the local Telegram Bot API, PostgreSQL, Redis, and the PO-token sidecar off the public internet.
- Expose a cookie broker only through a protected network path.
- Give invitations only to people you trust.
- Match download-size and worker-concurrency limits to the host.
- Rotate a credential immediately if it appears in logs, chat, Git, or an image layer.

The provided `.gitignore` and `.dockerignore` block common credential and artifact paths, but they do not replace operator review. CI runs secret and dependency scans on every push and pull request, plus a weekly vulnerability rescan.

Report vulnerabilities privately through [the security policy](SECURITY.md). Never place credentials, cookies, private URLs, user data, or raw production logs in a public issue.

<details>
<summary><strong>Runtime configuration</strong></summary>

### Required by the Compose deployment

| Variable | Purpose |
| --- | --- |
| `BOT_TOKEN` | Telegram bot credential |
| `POSTGRES_PASSWORD` | PostgreSQL password used by the stack |
| `TELEGRAM_API_ID` | local Telegram Bot API application ID |
| `TELEGRAM_API_HASH` | local Telegram Bot API application hash |

### Common overrides

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | direct process execution; Compose sets it automatically | required outside Compose |
| `REDIS_URL` | Redis connection URL | `redis://redis:6379/0` |
| `CUSTOM_API_URL` | Telegram Bot API endpoint | `https://api.telegram.org` in code |
| `YOUTUBE_POT_PROVIDER_URL` | BgUtils PO-token provider endpoint | `http://youtube-pot-provider:4416` in Compose |
| `ARTIFACT_ROOT` | shared download directory | `downloads` |
| `DOWNLOAD_MAX_FILE_SIZE` | maximum artifact size in bytes | `2000000000` |
| `MAX_PARALLEL_DOWNLOADS` | concurrent downloads per worker | `4` |
| `YTDLP_RESOLVE_TIMEOUT_SECONDS` | maximum yt-dlp metadata resolution time | `120` |
| `ARTIFACT_RETENTION_SECONDS` | completed artifact retention | `86400` |
| `UX_SELECTION_FLOW` | honor YouTube's **Always ask** picker | `true` |
| `SPOTIFY_BITRATE` | native Spotify bitrate: 96, 160, or 320 | `320` |

[.env.example](.env.example) is the safe Compose starting point. Complete runtime defaults live in [`Settings`](downloader_bot/bootstrap/settings.py).

</details>

<details>
<summary><strong>Architecture</strong></summary>

Dependencies point inward:

```text
bootstrap → adapters / infrastructure → application → domain
```

- `downloader_bot/domain` — immutable models and the job state machine;
- `downloader_bot/application` — use cases and typed ports;
- `downloader_bot/adapters/telegram` — aiogram routing, presentation, callbacks, and delivery;
- `downloader_bot/infrastructure` — PostgreSQL, Redis Streams, providers, downloads, and artifacts;
- `downloader_bot/bootstrap` — settings, dependency assembly, and process lifecycle.

PostgreSQL is canonical state. Redis Streams carries work and progress. Selection requests survive restarts, confirmation is atomic, active jobs are deduplicated, pending Redis messages can be reclaimed, and completed artifacts are retained for a bounded period.

See [architecture details](docs/architecture-v2.md), the [code map](docs/code-map.md), and the [behavior inventory](docs/behavior-inventory.md).

</details>

## 🧰 Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then create the locked Python 3.14 environment:

```bash
uv sync --locked

uv run ruff check downloader_bot acceptance_tests main.py
uv run ty check
uv run python -m compileall -q downloader_bot main.py
uv run pytest --cov=downloader_bot --cov-fail-under=80
```

Install and run the local quality gate:

```bash
uv run prek install
uv run prek run --all-files
scripts/security-check
```

| Tool | Role |
| --- | --- |
| uv | Python and dependency locking |
| Ruff | linting and formatting checks |
| ty | static type checking |
| prek | local pre-commit orchestration |
| pytest | network-free acceptance tests and coverage |
| Gitleaks / Trivy | secret, dependency, and deployment scans |

`uv.lock` is shared by local development and the runtime image. The acceptance suite mocks Telegram, providers, PostgreSQL repositories, and Redis Streams. Production-code coverage must remain at least 80%.

## 🚧 Current boundaries

- Self-hosted deployment only; there is no hosted service.
- There is no web administration panel.
- Provider availability can change when upstream sites change.
- Private or authentication-gated media needs valid operator-supplied cookies and may still fail.
- Native Spotify audio needs a deployment-owned Premium session.
- Telegram and local storage limits still apply.
- Cancellation is cooperative and may not interrupt every provider operation instantly.
- The bot does not automate login, CAPTCHA, 2FA, or provider account recovery.

<div align="center">

**One link in. Clean media out.**

MIT — see [LICENCE](LICENCE).

</div>
