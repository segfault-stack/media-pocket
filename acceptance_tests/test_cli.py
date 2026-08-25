from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import downloader_bot.__main__ as cli
from downloader_bot.domain import JobStage, Progress


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["bot", "worker"])
async def test_run_starts_selected_role_and_closes_container(monkeypatch, role: str) -> None:
    container = SimpleNamespace(close=AsyncMock())
    build = AsyncMock(return_value=container)
    run_bot = AsyncMock()
    run_worker = AsyncMock()
    monkeypatch.setattr(cli, "build_container", build)
    monkeypatch.setattr(cli, "run_bot", run_bot)
    monkeypatch.setattr(cli, "run_worker", run_worker)
    monkeypatch.setattr(
        cli.Settings,
        "from_env",
        lambda *, require_bot_token: SimpleNamespace(required=require_bot_token),
    )

    await cli._run(role)

    assert build.await_args.args[0].required is (role == "bot")
    (run_bot if role == "bot" else run_worker).assert_awaited_once_with(container)
    container.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_admin_disposes_engine(monkeypatch) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    repository = SimpleNamespace(add_admin=AsyncMock(return_value=True))
    monkeypatch.setattr(
        cli.Settings,
        "from_env",
        lambda *, require_bot_token: SimpleNamespace(database_url="postgresql://db"),
    )
    monkeypatch.setattr(cli, "create_engine", lambda _url: (engine, object()))
    monkeypatch.setattr(cli, "SqlAccessRepository", lambda _sessions: repository)

    assert await cli._add_admin(42) is True
    repository.add_admin.assert_awaited_once_with(42)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_hitmoz_download_reports_and_returns_paths(monkeypatch, tmp_path, capsys) -> None:
    class Adapter:
        async def resolve(self, _url, _preferences):
            return object()

    class Engine:
        async def download(self, _post, _job, report, cancellation):
            assert await cancellation.requested() is False
            await report(Progress("hitmoz", JobStage.DOWNLOADING, 50, item=2, item_count=3))
            return [SimpleNamespace(path=tmp_path / "track.mp3")]

    monkeypatch.setattr(cli, "HitMozPlatformAdapter", lambda *_args: Adapter())
    monkeypatch.setattr(cli, "HttpDownloadEngine", lambda *_args: Engine())

    paths = await cli.download_hitmoz(
        "https://example.com/song",
        tmp_path,
        client=SimpleNamespace(),
    )

    assert paths == (Path(tmp_path / "track.mp3"),)
    assert "Downloading track 2/3: 50%" in capsys.readouterr().out


def test_main_runs_migrations(monkeypatch) -> None:
    upgrade = Mock()
    monkeypatch.setattr(cli.command, "upgrade", upgrade)
    monkeypatch.setattr(cli, "Config", lambda path: path)

    cli.main(["migrate"])

    upgrade.assert_called_once_with("alembic.ini", "head")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["hitmoz"], "hitmoz requires a song or album URL"),
        (["admin"], "admin usage"),
        (["admin", "add", "0"], "positive Telegram user ID"),
    ],
)
def test_main_rejects_invalid_cli_arguments(arguments, message: str, capsys) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(arguments)
    assert message in capsys.readouterr().err


def test_main_prints_hitmoz_paths(monkeypatch, tmp_path, capsys) -> None:
    async def download(_url, _output):
        return (tmp_path / "one.mp3", tmp_path / "two.mp3")

    monkeypatch.setattr(cli, "download_hitmoz", download)

    cli.main(["hitmoz", "https://example.com/song", "--output", str(tmp_path)])

    output = capsys.readouterr().out
    assert str(tmp_path / "one.mp3") in output
    assert str(tmp_path / "two.mp3") in output


def test_main_handles_interrupted_hitmoz_download(monkeypatch, capsys) -> None:
    def interrupt(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", interrupt)

    cli.main(["hitmoz", "https://example.com/song"])

    assert "Re-run the same command" in capsys.readouterr().out


@pytest.mark.parametrize("created", [True, False])
def test_main_adds_admin(monkeypatch, created: bool, capsys) -> None:
    async def add_admin(_user_id):
        return created

    monkeypatch.setattr(cli, "_add_admin", add_admin)

    cli.main(["admin", "add", "42"])

    expected = "added" if created else "already exists"
    assert capsys.readouterr().out.strip() == f"Administrator 42: {expected}"


def test_main_starts_worker(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(cli, "_run", run)

    cli.main(["worker"])

    run.assert_awaited_once_with("worker")
