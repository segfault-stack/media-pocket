from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config

from downloader_bot.bootstrap.container import build_container
from downloader_bot.bootstrap.runtime import run_bot, run_worker
from downloader_bot.bootstrap.settings import Settings
from downloader_bot.domain import Job, Platform, Progress, UserPreferences
from downloader_bot.infrastructure.database import SqlAccessRepository, create_engine
from downloader_bot.infrastructure.download import HttpDownloadEngine
from downloader_bot.infrastructure.platforms import HitMozPlatformAdapter


async def _run(role: str) -> None:
    container = await build_container(
        Settings.from_env(require_bot_token=role == "bot")
    )
    try:
        await (run_bot(container) if role == "bot" else run_worker(container))
    finally:
        await container.close()


async def _add_admin(user_id: int) -> bool:
    settings = Settings.from_env(require_bot_token=False)
    engine, sessions = create_engine(settings.database_url)
    try:
        return await SqlAccessRepository(sessions).add_admin(user_id)
    finally:
        await engine.dispose()


class _CliCancellation:
    async def requested(self) -> bool:
        return False


async def download_hitmoz(
    url: str, output: Path, *, client: httpx.AsyncClient | None = None
) -> tuple[Path, ...]:
    """Resolve a HitMoz song or album URL and save its MP3 files beneath output."""

    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(60, read=600),
        limits=httpx.Limits(max_connections=8),
    )
    try:
        post = await HitMozPlatformAdapter(Platform.HITMOZ, active_client).resolve(
            url, UserPreferences()
        )
        job = Job("hitmoz", 0, 0, url, "hitmoz-cli")

        async def report(progress: Progress) -> None:
            print(
                f"\\rDownloading track {progress.item}/{progress.item_count}: "
                f"{progress.percent}%",
                end="",
                flush=True,
            )

        artifacts = await HttpDownloadEngine(active_client, output).download(
            post, job, report, _CliCancellation()
        )
        if artifacts:
            print()
        return tuple(Path(artifact.path) for artifact in artifacts)
    finally:
        if owns_client:
            await active_client.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role",
        choices=("bot", "worker", "migrate", "hitmoz", "admin"),
        nargs="?",
        default="bot",
    )
    parser.add_argument("url", nargs="?", help="HitMoz song or album URL")
    parser.add_argument("admin_user_id", nargs="?", type=int)
    parser.add_argument("--output", type=Path, default=Path("downloads"))
    args = parser.parse_args(argv)
    if args.role == "migrate":
        command.upgrade(Config("alembic.ini"), "head")
        return
    if args.role == "hitmoz":
        if not args.url:
            parser.error("hitmoz requires a song or album URL")
        try:
            paths = asyncio.run(download_hitmoz(args.url, args.output))
        except KeyboardInterrupt:
            print("\\nInterrupted. Re-run the same command to resume partial downloads.")
            return
        for path in paths:
            print(path)
        return
    if args.role == "admin":
        if args.url != "add" or args.admin_user_id is None:
            parser.error("admin usage: python -m downloader_bot admin add USER_ID")
        if args.admin_user_id <= 0:
            parser.error("USER_ID must be a positive Telegram user ID")
        created = asyncio.run(_add_admin(args.admin_user_id))
        status = "added" if created else "already exists"
        print(f"Administrator {args.admin_user_id}: {status}")
        return
    asyncio.run(_run(args.role))


if __name__ == "__main__":
    main()
