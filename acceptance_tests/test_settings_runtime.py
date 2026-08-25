from __future__ import annotations

import importlib

import pytest

from downloader_bot.bootstrap.settings import Settings


def test_importing_entrypoint_has_no_required_environment(monkeypatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert callable(importlib.import_module("main").main)


def test_typed_settings_parse_all_runtime_options() -> None:
    settings = Settings.from_env(
        {
            "BOT_TOKEN": "123:token",
            "DATABASE_URL": "postgresql://u:p@db/name",
            "ADMIN_IDS": "1, 2",
            "ARTIFACT_RETENTION_SECONDS": "42",
            "MAX_PARALLEL_DOWNLOADS": "4",
            "YTDLP_COOKIES_FILE": "/cookies/cookies.txt",
            "YTDLP_YOUTUBE_COOKIES_FILE": "/cookies/youtube.txt",
            "YTDLP_TIKTOK_COOKIES_FILE": "/cookies/tiktok.txt",
            "YTDLP_INSTAGRAM_COOKIES_FILE": "/cookies/instagram.txt",
            "YTDLP_X_COOKIES_FILE": "/cookies/x.txt",
        }
    )
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.admin_ids == frozenset({1, 2})
    assert settings.artifact_retention_seconds == 42
    assert settings.max_parallel_downloads == 4
    assert settings.cookies_file == "/cookies/cookies.txt"
    assert settings.youtube_cookies_file == "/cookies/youtube.txt"
    assert settings.tiktok_cookies_file == "/cookies/tiktok.txt"
    assert settings.instagram_cookies_file == "/cookies/instagram.txt"
    assert settings.x_cookies_file == "/cookies/x.txt"


def test_provider_cookie_files_fall_back_to_combined_file() -> None:
    settings = Settings.from_env(
        {
            "BOT_TOKEN": "123:token",
            "DATABASE_URL": "postgresql://u:p@db/name",
            "YTDLP_COOKIES_FILE": "/cookies/cookies.txt",
        }
    )
    assert settings.youtube_cookies_file == settings.cookies_file
    assert settings.tiktok_cookies_file == settings.cookies_file
    assert settings.instagram_cookies_file == settings.cookies_file
    assert settings.x_cookies_file == settings.cookies_file


def test_parallel_download_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="MAX_PARALLEL_DOWNLOADS"):
        Settings.from_env(
            {"BOT_TOKEN": "123:token", "DATABASE_URL": "postgresql://u:p@db/name", "MAX_PARALLEL_DOWNLOADS": "0"}
        )


def test_worker_role_does_not_require_bot_token() -> None:
    settings = Settings.from_env(
        {"DATABASE_URL": "postgresql://u:p@db/name"}, require_bot_token=False
    )
    assert settings.bot_token == ""


def test_bot_role_requires_token() -> None:
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        Settings.from_env({"DATABASE_URL": "postgresql://u:p@db/name"})
