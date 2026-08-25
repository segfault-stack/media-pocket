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

RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
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
COPY --chown=appuser:appgroup . .

RUN chown appuser:appgroup /app \
    && install -d -o appuser -g appgroup /app/downloads /app/logs /app/cookies /app/spotify \
    && python scripts/docker_media_smoke.py

ENTRYPOINT ["python", "container_entrypoint.py"]
CMD ["python", "main.py"]
