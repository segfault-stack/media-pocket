from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_url: str
    redis_url: str = "redis://redis:6379/0"
    artifact_root: Path = Path("downloads")
    worker_name: str = "worker-1"
    telegram_api_url: str = "https://api.telegram.org"
    cookies_file: str | None = None
    youtube_cookies_file: str | None = None
    youtube_pot_provider_url: str | None = None
    tiktok_cookies_file: str | None = None
    instagram_cookies_file: str | None = None
    x_cookies_file: str | None = None
    max_file_size: int = 2_000_000_000
    max_parallel_downloads: int = 4
    reclaim_idle_ms: int = 60_000
    artifact_retention_seconds: int = 86_400
    admin_ids: frozenset[int] = frozenset()
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    spotify_market: str = "US"
    spotify_command: str | None = "spotify-streamer"
    spotify_cache_dir: Path = Path("spotify")
    spotify_bitrate: int = 320
    spotify_resolve_timeout_seconds: int = 120
    ux_selection_flow: bool = True

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, require_bot_token: bool = True
    ) -> Settings:
        values = environ if environ is not None else os.environ
        bot_token = values.get("BOT_TOKEN", "")
        database_url = values.get("DATABASE_URL", "")
        if require_bot_token and not bot_token:
            raise ValueError("BOT_TOKEN is required for the bot process")
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        max_parallel_downloads = int(values.get("MAX_PARALLEL_DOWNLOADS", "4"))
        if max_parallel_downloads < 1:
            raise ValueError("MAX_PARALLEL_DOWNLOADS must be at least 1")
        spotify_bitrate = int(values.get("SPOTIFY_BITRATE", "320"))
        if spotify_bitrate not in {96, 160, 320}:
            raise ValueError("SPOTIFY_BITRATE must be 96, 160, or 320")
        spotify_resolve_timeout_seconds = int(
            values.get("SPOTIFY_RESOLVE_TIMEOUT_SECONDS", "120")
        )
        if spotify_resolve_timeout_seconds < 1:
            raise ValueError("SPOTIFY_RESOLVE_TIMEOUT_SECONDS must be at least 1")
        cookies_file = values.get("YTDLP_COOKIES_FILE") or None
        return cls(
            bot_token=bot_token,
            database_url=database_url,
            redis_url=values.get("REDIS_URL", "redis://redis:6379/0"),
            artifact_root=Path(values.get("ARTIFACT_ROOT", "downloads")),
            worker_name=values.get("WORKER_NAME", f"worker-{os.getpid()}"),
            telegram_api_url=values.get("CUSTOM_API_URL", "https://api.telegram.org"),
            cookies_file=cookies_file,
            youtube_cookies_file=values.get("YTDLP_YOUTUBE_COOKIES_FILE")
            or cookies_file,
            youtube_pot_provider_url=values.get("YOUTUBE_POT_PROVIDER_URL") or None,
            tiktok_cookies_file=values.get("YTDLP_TIKTOK_COOKIES_FILE") or cookies_file,
            instagram_cookies_file=values.get("YTDLP_INSTAGRAM_COOKIES_FILE")
            or cookies_file,
            x_cookies_file=values.get("YTDLP_X_COOKIES_FILE") or cookies_file,
            max_file_size=int(values.get("DOWNLOAD_MAX_FILE_SIZE", "2000000000")),
            max_parallel_downloads=max_parallel_downloads,
            reclaim_idle_ms=int(values.get("REDIS_RECLAIM_IDLE_MS", "60000")),
            artifact_retention_seconds=int(
                values.get("ARTIFACT_RETENTION_SECONDS", "86400")
            ),
            admin_ids=_parse_ids(
                values.get("ADMIN_IDS") or values.get("ADMIN_ID") or ""
            ),
            spotify_client_id=values.get("SPOTIFY_CLIENT_ID") or None,
            spotify_client_secret=values.get("SPOTIFY_CLIENT_SECRET") or None,
            spotify_market=values.get("SPOTIFY_MARKET", "US"),
            spotify_command=values.get("SPOTIFY_COMMAND", "spotify-streamer") or None,
            spotify_cache_dir=Path(values.get("SPOTIFY_CACHE_DIR", "spotify")),
            spotify_bitrate=spotify_bitrate,
            spotify_resolve_timeout_seconds=spotify_resolve_timeout_seconds,
            ux_selection_flow=_parse_bool(values.get("UX_SELECTION_FLOW", "true")),
        )


def _parse_ids(value: str) -> frozenset[int]:
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


def _parse_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}
