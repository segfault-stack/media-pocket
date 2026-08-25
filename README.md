<div align="center">

# 📥 Media Pocket

### Send a link. Get clean media back.

**An invite-only, self-hosted Telegram downloader for video, audio, social posts, albums, playlists, and batches.**

[![License](https://img.shields.io/github/license/segfault-stack/media-pocket)](LICENCE)
![Python](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

[Quick start](#-quick-start) ·
[How it works](#-what-happens-after-a-link) ·
[Platforms](#-platforms-and-formats) ·
[Access](#-invite-only-by-design) ·
[Spotify](#-spotify-native-audio) ·
[Security](#-security)

</div>

---

Media Pocket turns Telegram into a private media inbox. Send one link or a batch and let workers handle extraction, conversion, and delivery. Provider-native defaults avoid a menu on every request, while YouTube can be configured per user for video, audio, or an explicit format prompt.

> **link → provider-native mode → live progress → clean media**

The original message is removed when the bot has permission. Delivered media contains only the file or media group; source links and follow-up actions stay in a separate message.

The bot is deliberately not open to everyone. An administrator creates short invitation codes, and the same access check protects private chats, groups, Telegram Business messages, callbacks, and inline mode.

> [!WARNING]
> Media Pocket does not bypass provider access controls. Private, deleted, region-restricted, or authentication-gated content may require valid cookies or may remain unavailable. Operators are responsible for complying with provider rules and local law.

> [!TIP]
> [Media Cookie Broker](https://github.com/segfault-stack/media-cookie-broker) is an optional companion project for keeping provider cookie jars refreshed. Media Pocket does not bundle it; operators configure the broker and cookie-sync connection separately.

## Bot and worker, separated

> **Telegram → Bot → Redis queue → Worker → Shared artifacts → Bot → Telegram**

Only the bot process holds a Telegram client. Workers resolve and download media, report structured progress through Redis, and leave delivery to the bot.

---

## 🚀 Quick start

### 🤖 1. Prepare the Telegram bot

Create a bot with [@BotFather](https://t.me/BotFather) and keep its token private.

For inline downloads, use `/setinline`, then `/setinlinefeedback` and choose **Enabled**. Media Pocket confirms an inline download from Telegram's chosen-result update, so sampled feedback modes such as `1/10` or `1/100` are not suitable.

If the bot must receive ordinary links in groups without being mentioned, adjust its group privacy setting in BotFather as well.

The bundled local Telegram Bot API service also needs `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org/).

### 🐳 2. Configure the server

```bash
git clone https://github.com/segfault-stack/media-pocket.git
cd media-pocket
git submodule update --init --recursive

cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
```

Set at least these values in `.env`:

```env
BOT_TOKEN=replace-with-bot-token
POSTGRES_PASSWORD=replace-with-a-long-random-password
TELEGRAM_API_ID=replace-with-api-id
TELEGRAM_API_HASH=replace-with-api-hash
```

Build the image and initialize the database:

```bash
docker compose build
docker compose up -d postgres redis telegram-bot-api
docker compose run --rm migrate
```

The application image contains only the Python environment and media toolchain. Compose bind-mounts the checkout at `/app` read-only, with separate writable volumes for downloads, logs, cookies, and Spotify state. Restart the bot and workers after source changes; rebuild the image only when dependencies or toolchain versions change.

### YouTube extraction stack

The image pins yt-dlp with its default extras and recommended `curl-cffi` browser-impersonation transport, the yt-dlp EJS challenge scripts, Deno, and FFmpeg/ffprobe. Compose also starts the matching Deno-based BgUtils PO-token provider, while the Python plugin connects yt-dlp to that private sidecar automatically. YouTube extraction uses the `mweb` client so GVS PO tokens are requested automatically. `YOUTUBE_POT_PROVIDER_URL` can override the internal endpoint.

This is compatibility plumbing, not an access-control bypass: cookies may still be required for account-gated material, rate limits still apply, and YouTube can change extraction requirements without notice.

### 🔐 3. Add the first administrator

The bot refuses to start until PostgreSQL contains at least one administrator:

```bash
docker compose run --rm migrate \
  python -m downloader_bot admin add YOUR_TELEGRAM_USER_ID
```

Start the bot and one or more workers:

```bash
docker compose up -d --scale worker=2
```

Open the bot, run `/admin`, create an invitation code, and share it with the people who should have access.

---

## 🔎 What happens after a link?

```text
link received
      │
      ▼
┌─────────────────────┐
│ natural mode        │
│ or YouTube ask card │
└──────────┬──────────┘
           │ immediate / Download
           ▼
┌─────────────────────┐
│ PostgreSQL snapshot │
│ atomic confirmation │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Redis worker queue  │
│ retry + cancellation│
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ progress in one msg │
│ 64% · speed · ETA   │
└──────────┬──────────┘
           │
           ▼
      media delivered
           │
           ├── status message disappears
           └── actions arrive separately
```

Ordinary links start immediately: audio-first services use audio, social posts keep their media type, and video sources use video. YouTube follows the user's saved **Video**, **Audio**, or **Always ask** setting. Only **Always ask** opens the 15-minute selection card.

Explicit `/audio URL`, `/video URL`, `!a`, `!audio`, `!mp3`, and `!music` commands always override the saved mode and start immediately.

For an automatic batch, each link keeps its provider-native mode. If the batch contains YouTube and **Always ask** is enabled, one selection applies to the batch; an inapplicable choice falls back to the nearest valid mode for that item.

Failures remain visible as a compact card with a human-readable reason, a next step, retry, format change, and help actions. Tracebacks and provider internals are not shown to users.

---

## 🌐 Platforms and formats

| Source | What Media Pocket handles |
| --- | --- |
| YouTube | video, audio, playlists, quality selection |
| TikTok | media posts, audio extraction, file delivery |
| Instagram | media posts and albums, audio extraction, file delivery |
| X / Twitter | media posts, audio extraction, file delivery |
| Threads | media posts, audio extraction, file delivery |
| Pinterest | media posts, audio extraction, file delivery |
| SoundCloud | audio and file delivery |
| Spotify | tracks, albums, and playlists through native audio or fallback |
| Zaycev.net | tracks and file delivery |
| HitMoz | tracks, albums, and direct CLI downloads |
| Generic HTTP(S) | provider resolution through yt-dlp where supported |

Video sources offer **Video** or **Audio**, **Best / 1080p / 720p / 480p**, and **Media** or **File** delivery where those choices apply. Social posts avoid a fake resolution selector. Audio-first providers show only relevant audio and file controls.

Video delivery is normalized to streaming-compatible MP4 with H.264/AAC. Audio delivery is normalized to M4A with AAC-LC and carries title, performer, duration, and thumbnail metadata when the provider supplies it.

---

## 🎛️ Telegram workflow

- `/start` — onboarding, inline shortcut, settings, help, group installation, and sharing;
- `/help` — platforms, formats, batches, inline mode, and limitations;
- `/settings` — download quality, delivery, action buttons, source cleanup, and compact progress;
- `/status` — the current user's active and recent jobs, cancellation, and retry;
- `/audio URL` and `/video URL` — immediate format-specific downloads;
- `@your_bot URL` — inline submission for already invited users;
- `/admin` — invitation management for administrators.

Single results can expose contextual actions such as **Get audio**, **Get video**, **Send as file**, **Source**, and **Share**. Albums are delivered as media groups followed by one compact action card.

---

## 🎟️ Invite-only by design

An administrator can create:

- a one-use code that never expires and can be redeemed once;
- a timed code that can be redeemed by multiple users until its expiry.

Users send the bare code or `/redeem CODE`. Successful redemption grants permanent access until the database record is changed by the operator.

Inline queries are not a side door: unauthorized users receive only a prompt to open the private chat and enter an invitation code. Callback ownership is also checked, so another user cannot confirm, cancel, or retry someone else's request.

---

## 🎧 Spotify native audio

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

Spotify credentials are stored in PostgreSQL. The librespot cache lives in the `spotify_cache` Docker volume. `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are optional and support collection expansion for the fallback path; they are not Premium account credentials.

---

## 🍪 Optional provider cookies

yt-dlp can read a combined Netscape cookie jar or provider-specific files from `cookies/`. Treat every cookie file as a password and never commit it.

The optional `cookie-broker` Compose profile can run a separately configured `cookie-sync` image and write updated cookie jars into the shared directory:

```bash
# Configure COOKIE_BROKER_IMAGE, BROKER_URL, BROKER_USERNAME,
# and secrets/cookie-broker-password first.
docker compose --profile cookie-broker up -d cookie-sync
```

The companion [Media Cookie Broker](https://github.com/segfault-stack/media-cookie-broker)
is not bundled with Media Pocket. Its image, endpoint, credentials, and network policy
are deployment choices.

---

## 🔐 Security

Media Pocket handles bot tokens, provider sessions, browser cookies, and user-submitted URLs.

Keep these rules:

- never commit `.env`, `secrets/`, cookie jars, Spotify sessions, database dumps, or downloaded media;
- use a long random PostgreSQL password;
- keep the local Telegram Bot API and PostgreSQL off the public internet;
- expose a cookie broker only through a protected network path;
- give invitations only to people you trust;
- keep the bot's download-size and worker-concurrency limits appropriate for the host;
- rotate a credential immediately if it appears in logs, chat, Git, or an image layer.

The provided `.gitignore` and `.dockerignore` block common credential and artifact paths, but they do not replace operator review.

---

<details>
<summary><strong>🏗️ Architecture</strong></summary>

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

See [architecture details](docs/architecture-v2.md) and the [behavior inventory](docs/behavior-inventory.md).

</details>

<details>
<summary><strong>⚙️ Runtime configuration</strong></summary>

| Variable | Purpose | Default |
| --- | --- | --- |
| `BOT_TOKEN` | Telegram bot credential | required for bot |
| `DATABASE_URL` | PostgreSQL connection URL outside Compose | required |
| `POSTGRES_PASSWORD` | Password used by the Compose stack | required in Compose |
| `REDIS_URL` | Redis connection URL | `redis://redis:6379/0` |
| `CUSTOM_API_URL` | Telegram Bot API endpoint | `https://api.telegram.org` in code |
| `ARTIFACT_ROOT` | Shared download directory | `downloads` |
| `DOWNLOAD_MAX_FILE_SIZE` | Maximum artifact size in bytes | `2000000000` |
| `MAX_PARALLEL_DOWNLOADS` | Concurrent downloads per worker | `4` |
| `ARTIFACT_RETENTION_SECONDS` | Completed artifact retention | `86400` |
| `UX_SELECTION_FLOW` | Honor YouTube's `Always ask` picker | `true` |
| `SPOTIFY_BITRATE` | Native Spotify bitrate: 96, 160, or 320 | `320` |

[.env.example](.env.example) is the safe Compose starting point. The complete runtime defaults are implemented in [`Settings`](downloader_bot/bootstrap/settings.py).

</details>

---

## 🧪 Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then create the locked development environment:

```bash
uv sync --locked

uv run ruff check downloader_bot acceptance_tests main.py
uv run ty check
uv run python -m compileall -q downloader_bot main.py
uv run pytest --cov=downloader_bot --cov-fail-under=80
```

Install the fast Git hook runner once per checkout and run the same gate manually when needed:

```bash
uv run prek install
uv run prek run --all-files
scripts/security-check
```

`uv.lock` is the single dependency lock for both the runtime image and local development. Prek checks file integrity, the lockfile, Ruff, and ty before commits. The security script runs pinned Gitleaks and Trivy containers against Git history, dependencies, and deployment configuration; its first run downloads the scanner images and vulnerability database. GitHub Actions runs these checks and the network-free acceptance suite on pushes and pull requests, with a weekly vulnerability rescan.

The development stack is intentionally small:

- **uv** manages Python 3.14 and the fully locked environment;
- **Ruff** handles linting and formatting checks;
- **ty** performs static type checking;
- **prek** runs the local pre-commit gate;
- **pytest** enforces at least 80% production-code coverage;
- **Gitleaks** and **Trivy** scan committed history, dependency locks, and deployment files.

Docker builds the reproducible runtime environment and media toolchain, not a private copy of the application source. Compose mounts the checkout read-only and gives only runtime data directories writable volumes. This keeps source edits fast during self-hosted development while preserving a pinned environment through `uv.lock` and the container image.

---

## 🚧 Current boundaries

- self-hosted deployment only; there is no hosted service;
- no web administration panel;
- provider availability can change when upstream sites change;
- private or authentication-gated media needs valid operator-supplied cookies and may still fail;
- native Spotify audio needs a deployment-owned Premium session;
- Telegram and local storage limits still apply;
- cancellation is cooperative and may not interrupt every provider operation instantly;
- the bot does not automate login, CAPTCHA, 2FA, or provider account recovery.

---

<div align="center">

### 📥 One link in. Clean media out.

MIT — see [LICENCE](LICENCE).

</div>
