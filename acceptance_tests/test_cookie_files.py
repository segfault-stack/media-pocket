from __future__ import annotations

from pathlib import Path

from downloader_bot.infrastructure.cookies import writable_cookie_file


def test_broker_cookie_snapshot_is_copied_to_a_disposable_writable_jar(
    tmp_path: Path,
) -> None:
    source = tmp_path / "youtube.txt"
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    source.chmod(0o400)

    with writable_cookie_file(str(source)) as cookie_file:
        assert cookie_file and cookie_file != str(source)
        temporary = Path(cookie_file)
        assert temporary.read_text(encoding="utf-8") == source.read_text(
            encoding="utf-8"
        )
        temporary.write_text("updated by yt-dlp", encoding="utf-8")

    assert not temporary.exists()
    assert source.read_text(encoding="utf-8") == "# Netscape HTTP Cookie File\n"


def test_missing_or_unconfigured_cookie_file_is_preserved() -> None:
    with writable_cookie_file(None) as cookie_file:
        assert cookie_file is None
    with writable_cookie_file("/missing/cookies.txt") as cookie_file:
        assert cookie_file == "/missing/cookies.txt"
