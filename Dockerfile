FROM mwader/static-ffmpeg:8.1.1 AS ffmpeg
FROM denoland/deno:bin-2.9.4 AS deno

FROM rust:1.85-bookworm AS spotify-builder

WORKDIR /build
COPY third_party/librespot ./third_party/librespot
COPY tools/spotify-streamer ./tools/spotify-streamer
RUN cargo build --manifest-path tools/spotify-streamer/Cargo.toml --release --locked

FROM python:3.14-slim AS builder

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN python -m venv "$VIRTUAL_ENV"

COPY requirements.txt /tmp/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --disable-pip-version-check -r /tmp/requirements.txt \
    && rm -rf "$VIRTUAL_ENV"/lib/python*/site-packages/pip* \
    && find "$VIRTUAL_ENV" -type d -name __pycache__ -prune -exec rm -rf '{}' +

FROM python:3.14-slim AS runtime

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

RUN chown appuser:appgroup /app \
    && python -c "from importlib.metadata import version; import aiogram, asyncpg, curl_cffi, httpx, psycopg2, redis, sqlalchemy, yt_dlp, yt_dlp_ejs; version('bgutil-ytdlp-pot-provider')" \
    && ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null \
    && deno eval "if (1 + 1 !== 2) Deno.exit(1)" \
    && spotify-streamer --version

ENTRYPOINT ["python", "/app/container_entrypoint.py"]
CMD ["python", "-m", "downloader_bot", "bot"]
