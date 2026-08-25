FROM mwader/static-ffmpeg:9.0.1 AS ffmpeg
FROM denoland/deno:bin-2.9.5 AS deno
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM rust:1.98-bookworm AS spotify-builder

WORKDIR /build
COPY third_party/librespot ./third_party/librespot
COPY tools/spotify-streamer ./tools/spotify-streamer
RUN cargo build --manifest-path tools/spotify-streamer/Cargo.toml --release --locked

FROM python:3.14.7-slim AS builder

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=uv /uv /uvx /bin/

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project \
    && find "$VIRTUAL_ENV" -type d -name __pycache__ -prune -exec rm -rf '{}' +

FROM python:3.14.7-slim AS runtime

ENV TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN addgroup --system --gid 1000 appgroup \
    && adduser --system --uid 1000 --ingroup appgroup --home /app appuser

COPY --from=builder /opt/venv /opt/venv
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=spotify-builder /build/tools/spotify-streamer/target/release/spotify-streamer /usr/local/bin/spotify-streamer

RUN install -d -o appuser -g appgroup \
        /app/downloads /app/logs /app/cookies /app/spotify \
    && chown appuser:appgroup /app \
    && python -c "from importlib.metadata import version; import aiogram, asyncpg, curl_cffi, httpx, psycopg2, redis, sqlalchemy, yt_dlp, yt_dlp_ejs; version('bgutil-ytdlp-pot-provider')" \
    && ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null \
    && deno eval "if (1 + 1 !== 2) Deno.exit(1)" \
    && spotify-streamer --version

ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["python", "-m", "downloader_bot", "bot"]
