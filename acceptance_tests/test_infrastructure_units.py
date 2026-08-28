from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from downloader_bot.__main__ import download_hitmoz
from downloader_bot.domain import (
    DeliveryMode,
    ErrorCode,
    Job,
    JobStage,
    MediaAsset,
    MediaKind,
    MediaPost,
    Platform,
    PlaylistScope,
    Progress,
    SelectionMode,
    SelectionRequest,
    UserPreferences,
)
from downloader_bot.domain.errors import DownloadError
from downloader_bot.infrastructure.artifacts import FileArtifactStore
from downloader_bot.infrastructure.database import (
    Base,
    JobRow,
    PreferencesRow,
    SelectionRow,
    SqlSelectionRepository,
    _decode_artifacts,
    _decode_preferences,
    _decode_selection_urls,
    _encode_selection_urls,
    _job,
    _job_values,
    _preferences,
    _selection,
    _selection_values,
)
from downloader_bot.infrastructure.download import (
    _YTDLP_PROGRESS_PREFIX,
    HttpDownloadEngine,
    _audio_strategy,
    _download_headers,
    _is_telegram_compatible_video,
    _next_chunk,
    _suffix,
    _total_size,
    _transcode_audio,
    _transcode_video,
)
from downloader_bot.infrastructure.redis_streams import (
    RedisJobQueue,
    RedisProgressBus,
    _text,
)


class Cancellation:
    def __init__(self, value=False) -> None:
        self.value = value

    async def requested(self):
        return self.value


@pytest.mark.asyncio
async def test_slow_http_read_can_be_cancelled_between_chunks(monkeypatch) -> None:
    class SlowChunks:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "downloader_bot.infrastructure.download._CANCELLATION_POLL_SECONDS",
        0.01,
    )
    with pytest.raises(DownloadError) as raised:
        await _next_chunk(SlowChunks(), Cancellation(True))
    assert raised.value.code is ErrorCode.CANCELLED


@pytest.mark.asyncio
async def test_file_artifact_store_round_trip_and_cleanup(tmp_path) -> None:
    media = tmp_path / "job" / "x.mp4"
    media.parent.mkdir()
    media.write_bytes(b"data")
    from downloader_bot.domain import DownloadArtifact

    artifact = DownloadArtifact(media, MediaKind.VIDEO, 4, "sum", "video/mp4")
    store = FileArtifactStore(tmp_path)
    await store.persist("job", (artifact,))
    assert await store.get("job") == (artifact,)
    await store.persist("cached-job", (artifact,))
    cached = await store.get("cached-job")
    assert cached[0].path != artifact.path
    assert Path(cached[0].path).read_bytes() == b"data"
    await store.cleanup("job")
    await store.cleanup("cached-job")
    assert not media.parent.exists()


@pytest.mark.asyncio
async def test_http_engine_downloads_and_hashes_without_network(tmp_path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "cdn.example"
        return httpx.Response(200, content=b"media", headers={"content-length": "5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        engine = HttpDownloadEngine(client, tmp_path, max_file_size=10)
        events = []

        async def report(progress):
            events.append(progress)

        result = await engine.download(
            MediaPost(
                "https://example.com/post",
                Platform.GENERIC,
                (MediaAsset("https://cdn.example/x.bin", MediaKind.DOCUMENT),),
            ),
            Job("job", 1, 1, "https://example.com/post", "key"),
            report,
            Cancellation(),
        )
    assert result[0].size == 5 and Path(result[0].path).read_bytes() == b"media"
    assert any(event.stage is JobStage.DOWNLOADING and event.percent == 99 for event in events)
    assert events[-1].stage is JobStage.PROCESSING
    assert events[-1].detail == "preparing_document"


@pytest.mark.asyncio
async def test_http_engine_tries_direct_media_mirrors(tmp_path) -> None:
    hosts = []

    def respond(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "2ch.life":
            return httpx.Response(403)
        return httpx.Response(
            200,
            content=b"video",
            headers={"content-length": "5", "content-type": "video/mp4"},
        )

    asset = MediaAsset(
        "https://2ch.life/b/src/video.mp4",
        MediaKind.VIDEO,
        fallback_urls=(
            "https://2ch.hk/b/src/video.mp4",
            "https://2ch.su/b/src/video.mp4",
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await HttpDownloadEngine(client, tmp_path).download(
            MediaPost("https://2ch.life/b/res/1.html", Platform.TWO_CH, (asset,)),
            Job(
                "mirror-job",
                1,
                1,
                "https://2ch.life/b/src/video.mp4",
                "key",
                preferences=UserPreferences(document_mode=True),
            ),
            lambda _progress: asyncio.sleep(0),
            Cancellation(),
        )

    assert hosts == ["2ch.life", "2ch.hk"]
    assert result[0].kind is MediaKind.VIDEO
    assert Path(result[0].path).read_bytes() == b"video"


@pytest.mark.asyncio
async def test_force_audio_changes_video_artifact_kind_and_keeps_metadata(
    monkeypatch, tmp_path
) -> None:
    async def transcode(source, _cancellation, **metadata):
        assert callable(metadata.pop("report"))
        assert metadata.pop("strategy") == "transcode"
        assert metadata == {"title": "Track", "author": "Artist", "cover": None}
        output = source.with_suffix(".m4a")
        source.replace(output)
        return output

    monkeypatch.setattr(
        "downloader_bot.infrastructure.download._transcode_audio", transcode
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=b"media", headers={"content-length": "5"}
            )
        )
    ) as client:
        result = await HttpDownloadEngine(client, tmp_path).download(
            MediaPost(
                "https://example.com/post",
                Platform.GENERIC,
                (
                    MediaAsset(
                        "https://cdn.example/video.mp4",
                        MediaKind.VIDEO,
                        title="Track",
                        author="Artist",
                        duration_ms=12_000,
                    ),
                ),
            ),
            Job(
                "audio-job",
                1,
                1,
                "https://example.com/post",
                "key",
                audio_only=True,
            ),
            lambda _progress: asyncio.sleep(0),
            Cancellation(),
        )
    assert result[0].kind is MediaKind.AUDIO
    assert result[0].title == "Track"
    assert result[0].author == "Artist"
    assert result[0].duration_ms == 12_000
    assert Path(result[0].path).name == "Artist - Track.m4a"


@pytest.mark.asyncio
async def test_file_delivery_preserves_original_without_transcoding(
    monkeypatch, tmp_path
) -> None:
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("document delivery must preserve the downloaded file")

    monkeypatch.setattr(
        "downloader_bot.infrastructure.download._transcode_video", unexpected
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=b"original", headers={"content-length": "8"}
            )
        )
    ) as client:
        result = await HttpDownloadEngine(client, tmp_path).download(
            MediaPost(
                "https://example.com/post",
                Platform.GENERIC,
                (MediaAsset("https://cdn.example/video.webm", MediaKind.VIDEO),),
            ),
            Job(
                "document-job",
                1,
                1,
                "https://example.com/post",
                "key",
                preferences=UserPreferences(document_mode=True),
            ),
            lambda _progress: asyncio.sleep(0),
            Cancellation(),
        )
    assert Path(result[0].path).suffix == ".webm"
    assert Path(result[0].path).read_bytes() == b"original"


@pytest.mark.asyncio
async def test_prepared_media_size_is_checked_after_conversion(monkeypatch, tmp_path) -> None:
    async def strategy(_target):
        return "transcode"

    async def expand(target, _cancellation, **_kwargs):
        target.write_bytes(b"x" * 20)
        return target

    monkeypatch.setattr(
        "downloader_bot.infrastructure.download._video_strategy", strategy
    )
    monkeypatch.setattr(
        "downloader_bot.infrastructure.download._transcode_video", expand
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=b"small", headers={"content-length": "5"}
            )
        )
    ) as client:
        with pytest.raises(DownloadError) as raised:
            await HttpDownloadEngine(client, tmp_path, max_file_size=10).download(
                MediaPost(
                    "https://example.com/post",
                    Platform.GENERIC,
                    (MediaAsset("https://cdn.example/video.mp4", MediaKind.VIDEO),),
                ),
                Job("expanded", 1, 1, "https://example.com/post", "key"),
                lambda _progress: asyncio.sleep(0),
                Cancellation(),
            )
    assert raised.value.code is ErrorCode.TOO_LARGE


@pytest.mark.asyncio
async def test_ytdlp_fallback_downloads_with_mweb_and_readable_name(
    monkeypatch, tmp_path
) -> None:
    command: tuple[object, ...] = ()
    copied_cookie_file: Path | None = None

    class Stderr:
        async def read(self):
            return b""

    class Stdout:
        def __init__(self) -> None:
            payload = {
                "status": "downloading",
                "downloaded_bytes": 512,
                "total_bytes_estimate": 1024,
                "speed": 256,
                "eta": 2,
                "elapsed": 2.5,
            }
            self.lines = [
                f"{_YTDLP_PROGRESS_PREFIX}{json.dumps(payload)}\n".encode()
            ]

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class Process:
        returncode = None
        stderr = Stderr()
        stdout = Stdout()

        def __init__(self, template: Path) -> None:
            self.template = template

        async def wait(self):
            output = Path(str(self.template).replace("%(ext)s", "webm"))
            output.write_bytes(b"audio")
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = -15

    async def create(*args, **_kwargs):
        nonlocal command, copied_cookie_file
        command = args
        copied_cookie_file = Path(args[args.index("--cookies") + 1])
        assert copied_cookie_file.read_text(encoding="utf-8") == "cookie snapshot"
        template = Path(args[args.index("--output") + 1])
        return Process(template)

    monkeypatch.delenv("YTDLP_YOUTUBE_PLAYER_CLIENT", raising=False)
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    cookie_snapshot = tmp_path / "youtube.txt"
    cookie_snapshot.write_text("cookie snapshot", encoding="utf-8")
    cookie_snapshot.chmod(0o400)
    events = []

    async def report(event):
        events.append(event)

    async with httpx.AsyncClient() as client:
        output = await HttpDownloadEngine(
            client,
            tmp_path,
            youtube_pot_provider_url="http://youtube-pot-provider:4416",
        )._download_with_ytdlp(
            MediaAsset(
                "https://googlevideo.example/audio",
                MediaKind.AUDIO,
                title="Track",
                author="Artist",
                extractor_url="https://youtube.com/watch?v=x",
                format_selector="bestaudio/best",
                cookies_file=str(cookie_snapshot),
            ),
            Job("job", 1, 1, "https://youtube.com/watch?v=x", "key", audio_only=True),
            1,
            2,
            tmp_path,
            report,
            Cancellation(),
        )
    assert output.name == "Artist - Track.webm"
    assert output.read_bytes() == b"audio"
    assert "youtube:player_client=mweb" in command
    assert "youtubepot-bgutilhttp:base_url=http://youtube-pot-provider:4416" in command
    assert str(cookie_snapshot) not in command
    assert copied_cookie_file is not None and not copied_cookie_file.exists()
    assert events[-1].item_count == 2
    assert events[-1].percent == 50
    assert events[-1].downloaded_bytes == 512
    assert events[-1].total_bytes == 1024
    assert events[-1].total_bytes_is_estimate
    assert events[-1].speed_bytes_per_second == 256
    assert events[-1].eta_seconds == 2
    assert "--progress-template" in command
    assert "--ignore-config" in command


@pytest.mark.asyncio
async def test_merged_provider_download_pipes_to_telegram_mp4(
    monkeypatch, tmp_path
) -> None:
    commands = []

    class Reader:
        def __init__(self, chunks=()) -> None:
            self.chunks = list(chunks)

        async def read(self, _size=-1):
            return self.chunks.pop(0) if self.chunks else b""

    class Writer:
        def __init__(self) -> None:
            self.content = bytearray()

        def write(self, chunk):
            self.content.extend(chunk)

        async def drain(self):
            return None

        def close(self):
            return None

    class Process:
        returncode = 0

        def __init__(self, *, stdout=None, stdin=None) -> None:
            self.stdout = stdout
            self.stdin = stdin
            self.stderr = Reader()

        async def wait(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

    async def create(*args, **_kwargs):
        commands.append(args)
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"telegram mp4")
            return Process(stdin=Writer())
        return Process(stdout=Reader((b"merged transport stream",)))

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    events = []
    async with httpx.AsyncClient() as client:
        output = await HttpDownloadEngine(client, tmp_path)._download_merged_with_pipe(
            MediaAsset(
                "https://provider.example/post/x",
                MediaKind.VIDEO,
                extractor_url="https://provider.example/post/x",
                format_selector="bestvideo+bestaudio/best",
                requires_extractor_download=True,
            ),
            Job("job", 1, 1, "https://provider.example/post/x", "key"),
            1,
            1,
            tmp_path,
            events.append,
            Cancellation(),
        )

    assert output.name == "000.mp4"
    assert output.read_bytes() == b"telegram mp4"
    assert commands[0][commands[0].index("--output") + 1] == "-"
    assert commands[1][0] == "ffmpeg"
    assert "libx264" in commands[1]
    assert "aac" in commands[1]
    assert "-shortest" in commands[1]
    assert not tuple(tmp_path.glob("*.ytdlp.*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "requires_extractor"),
    [(Platform.YOUTUBE, False), (Platform.INSTAGRAM, True)],
)
async def test_extractor_download_preserves_selected_formats(
    monkeypatch, tmp_path, platform, requires_extractor
) -> None:
    async def fallback(
        _self, _asset, _job, _item, _count, directory, _progress, _cancellation
    ):
        output = directory / "Artist - Track.webm"
        output.write_bytes(b"audio")
        return output

    async def transcode(source, _cancellation, **_metadata):
        output = source.with_suffix(".m4a")
        source.replace(output)
        return output

    monkeypatch.setattr(HttpDownloadEngine, "_download_with_ytdlp", fallback)
    monkeypatch.setattr(
        "downloader_bot.infrastructure.download._transcode_audio", transcode
    )

    def unexpected_http(_request):
        raise AssertionError("merged media must be downloaded by yt-dlp")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected_http)
    ) as client:
        result = await HttpDownloadEngine(client, tmp_path).download(
            MediaPost(
                "https://youtube.com/watch?v=x",
                platform,
                (
                    MediaAsset(
                        "https://googlevideo.example/audio",
                        MediaKind.AUDIO,
                        title="Track",
                        author="Artist",
                        extractor_url="https://youtube.com/watch?v=x",
                        requires_extractor_download=requires_extractor,
                    ),
                ),
            ),
            Job("fallback", 1, 1, "https://youtube.com/watch?v=x", "key"),
            lambda _progress: asyncio.sleep(0),
            Cancellation(),
        )
    assert result[0].kind is MediaKind.AUDIO
    assert Path(result[0].path).name == "Artist - Track.m4a"


@pytest.mark.asyncio
async def test_ytdlp_fallback_maps_downloader_failure(monkeypatch, tmp_path) -> None:
    class Stdout:
        async def readline(self):
            return b""

    class Stderr:
        async def read(self):
            return b"youtube download failed"

    class Process:
        returncode = 1
        stderr = Stderr()
        stdout = Stdout()

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    async with httpx.AsyncClient() as client:
        with pytest.raises(DownloadError, match="youtube download failed") as raised:
            await HttpDownloadEngine(client, tmp_path)._download_with_ytdlp(
                MediaAsset(
                    "https://googlevideo.example/audio",
                    MediaKind.AUDIO,
                    extractor_url="https://youtube.com/watch?v=x",
                ),
                Job("job", 1, 1, "https://youtube.com/watch?v=x", "key"),
                1,
                1,
                tmp_path,
                lambda _progress: asyncio.sleep(0),
                Cancellation(),
            )
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_http_engine_limits_parallel_album_downloads(tmp_path) -> None:
    active = 0
    maximum = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=b"media", headers={"content-length": "5"})

    assets = tuple(
        MediaAsset(f"https://cdn.example/{index}.bin", MediaKind.DOCUMENT, index=index)
        for index in range(5)
    )

    async def report(_progress: Progress) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await HttpDownloadEngine(
            client, tmp_path, max_parallel_downloads=2
        ).download(
            MediaPost("https://example.com/album", Platform.GENERIC, assets),
            Job("album", 1, 1, "https://example.com/album", "key"),
            report,
            Cancellation(),
        )
    assert maximum == 2
    assert [artifact.size for artifact in result] == [5] * 5


@pytest.mark.asyncio
async def test_http_engine_enforces_size_and_cancellation(tmp_path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=b"too-big", headers={"content-length": "7"}
            )
        )
    ) as client:
        engine = HttpDownloadEngine(client, tmp_path, max_file_size=3)
        post = MediaPost(
            "https://example.com", Platform.GENERIC, (MediaAsset("https://cdn.example/x", MediaKind.VIDEO),)
        )
        with pytest.raises(DownloadError):
            await engine.download(post, Job("big", 1, 1, post.source_url, "key"), lambda _p: None, Cancellation())
        with pytest.raises(DownloadError):
            await engine.download(post, Job("cancel", 1, 1, post.source_url, "key2"), lambda _p: None, Cancellation(True))


@pytest.mark.asyncio
async def test_http_engine_uses_ffmpeg_for_hls_playlists(monkeypatch, tmp_path) -> None:
    class Process:
        returncode = 0
        stderr = None

        async def communicate(self):
            return b"{}", b""

    async def create(*args, **_kwargs):
        Path(args[-1]).write_bytes(b"video")
        return Process()

    async def keep_video(target, _cancellation, **_kwargs):
        return target

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    monkeypatch.setattr(
        "downloader_bot.infrastructure.download._transcode_video", keep_video
    )
    post = MediaPost(
        "https://youtube.com/watch?v=x",
        Platform.YOUTUBE,
        (MediaAsset("https://cdn.example/manifest", MediaKind.VIDEO),),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"#EXTM3U",
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )
        )
    ) as client:
        result = await HttpDownloadEngine(client, tmp_path).download(
            post,
            Job("hls", 1, 1, post.source_url, "key"),
            lambda _progress: asyncio.sleep(0),
            Cancellation(),
        )
    assert Path(result[0].path).suffix == ".mp4"
    assert Path(result[0].path).read_bytes() == b"video"


@pytest.mark.asyncio
async def test_hitmoz_cli_downloads_each_album_track(monkeypatch, tmp_path) -> None:
    page = """
        <title>Album</title>
        <a href="/get/music/20260822/Artist_-_One_123456.mp3">one</a>
        <a href="/get/music/20260822/Artist_-_Two_654321.mp3">two</a>
    """

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/album/1":
            return httpx.Response(200, text=page)
        assert request.headers["referer"] == "https://eu.hitmoz.com/"
        return httpx.Response(200, content=b"mp3", headers={"content-length": "3"})

    async def transcode(source, _cancellation, **_metadata):
        output = source.with_suffix(".m4a")
        source.replace(output)
        return output

    monkeypatch.setattr("downloader_bot.infrastructure.download._transcode_audio", transcode)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        paths = await download_hitmoz("https://eu.hitmoz.com/album/1", tmp_path, client=client)
    assert [path.read_bytes() for path in paths] == [b"mp3", b"mp3"]


def test_download_helpers() -> None:
    assert _suffix("https://cdn.example/file.jpg", "photo") == ".jpg"
    assert _suffix("https://cdn.example/no-extension", "audio") == ".mp3"
    assert _total_size(httpx.Response(206, headers={"content-length": "5"}), 3) == 8
    assert _total_size(httpx.Response(200), 0) is None
    headers = _download_headers(
        MediaPost("https://hitmos.me/album/532028", Platform.HITMOZ, ()),
        "https://eu.hitmoz.com/get/music/20260822/track.mp3",
    )
    assert headers["Referer"] == "https://eu.hitmoz.com/"
    assert "Chrome/124" in headers["User-Agent"]


@pytest.mark.asyncio
async def test_audio_transcode_subprocess_success_failure_and_cancel(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"audio")

    class Stderr:
        async def read(self):
            return b"conversion failed"

    class Process:
        def __init__(self, output, returncode=None):
            self.returncode = returncode
            self.output = output
            self.stderr = Stderr()

        async def wait(self):
            if self.returncode is None:
                self.output.write_bytes(b"converted")
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

    async def success(*args, **_kwargs):
        return Process(Path(args[-1]))

    monkeypatch.setattr("asyncio.create_subprocess_exec", success)
    output = await _transcode_audio(source, Cancellation(), strategy="transcode")
    assert output.suffix == ".m4a" and output.read_bytes() == b"converted"

    source.write_bytes(b"audio")

    async def failure(*args, **_kwargs):
        return Process(Path(args[-1]), 1)

    monkeypatch.setattr("asyncio.create_subprocess_exec", failure)
    with pytest.raises(DownloadError, match="conversion failed"):
        await _transcode_audio(source, Cancellation(), strategy="transcode")

    async def pending(*args, **_kwargs):
        return Process(Path(args[-1]))

    monkeypatch.setattr("asyncio.create_subprocess_exec", pending)
    with pytest.raises(DownloadError):
        await _transcode_audio(
            source, Cancellation(True), strategy="transcode"
        )


@pytest.mark.asyncio
async def test_audio_transcode_embeds_cover_art(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.m4a"
    source.write_bytes(b"audio")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")
    command = ()

    class Process:
        returncode = None
        stderr = None

        async def wait(self):
            Path(command[-1]).write_bytes(b"tagged audio")
            self.returncode = 0
            return 0

    async def create(*args, **_kwargs):
        nonlocal command
        command = args
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    output = await _transcode_audio(
        source,
        Cancellation(),
        title="Track",
        author="Artist",
        cover=cover,
        strategy="passthrough",
    )

    assert output == source
    assert output.read_bytes() == b"tagged audio"
    assert command[command.index("-i", 4) + 1] == str(cover)
    assert command[command.index("-map", 6) + 1] == "0:a:0"
    assert "1:v:0" in command
    assert "attached_pic" in command
    assert "-vn" not in command


@pytest.mark.asyncio
async def test_audio_strategy_avoids_unnecessary_encoding(monkeypatch, tmp_path) -> None:
    codec = "aac"
    format_name = "mov,mp4,m4a,3gp,3g2,mj2"

    class ProbeProcess:
        returncode = 0

        async def communicate(self):
            payload = {
                "streams": [{"codec_name": codec}],
                "format": {"format_name": format_name},
            }
            return json.dumps(payload).encode(), b""

    async def probe_only(*args, **_kwargs):
        assert args[0] == "ffprobe"
        return ProbeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", probe_only)
    source = tmp_path / "source.m4a"
    source.write_bytes(b"audio")
    assert await _transcode_audio(source, Cancellation()) == source
    codec = "mp3"
    format_name = "mp3"
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    assert await _audio_strategy(source) == "passthrough"
    codec = "aac"
    assert await _audio_strategy(source) == "remux"
    codec = "opus"
    assert await _audio_strategy(source) == "transcode"


@pytest.mark.asyncio
async def test_video_transcode_limits_cpu_for_incompatible_media(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"video")
    commands: list[tuple[object, ...]] = []

    class ProbeProcess:
        returncode = 0

        async def communicate(self):
            return (
                json.dumps(
                    {
                        "format": {"format_name": "matroska,webm"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "vp9",
                                "pix_fmt": "yuv420p",
                            },
                            {"codec_type": "audio", "codec_name": "opus"},
                        ],
                    }
                ).encode(),
                b"",
            )

    class FfmpegProcess:
        returncode = None
        stderr = None

        async def wait(self):
            Path(commands[-1][-1]).write_bytes(b"converted")
            self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

    async def create(*args, **_kwargs):
        commands.append(args)
        return ProbeProcess() if args[0] == "ffprobe" else FfmpegProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    output = await _transcode_video(source, Cancellation())
    assert output.suffix == ".mp4" and output.read_bytes() == b"converted"
    ffmpeg_command = commands[-1]
    assert "libx264" in ffmpeg_command and "aac_low" in ffmpeg_command
    assert ffmpeg_command[ffmpeg_command.index("-preset") + 1] == "veryfast"
    assert ffmpeg_command[ffmpeg_command.index("-pix_fmt") + 1] == "yuv420p"
    assert ffmpeg_command[ffmpeg_command.index("-threads") + 1] == "2"


@pytest.mark.asyncio
async def test_video_transcode_stream_copies_compatible_mp4(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    commands: list[tuple[object, ...]] = []

    class ProbeProcess:
        returncode = 0

        async def communicate(self):
            return (
                json.dumps(
                    {
                        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "pix_fmt": "yuv420p",
                            },
                            {"codec_type": "audio", "codec_name": "aac"},
                            {
                                "codec_type": "video",
                                "codec_name": "mjpeg",
                                "pix_fmt": "yuvj420p",
                            },
                            {"codec_type": "audio", "codec_name": "opus"},
                        ],
                    }
                ).encode(),
                b"",
            )

    class FfmpegProcess:
        returncode = None
        stderr = None

        async def wait(self):
            Path(commands[-1][-1]).write_bytes(b"remuxed")
            self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

    async def create(*args, **_kwargs):
        commands.append(args)
        return ProbeProcess() if args[0] == "ffprobe" else FfmpegProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    output = await _transcode_video(source, Cancellation())

    assert output.read_bytes() == b"remuxed"
    ffmpeg_command = commands[-1]
    assert ffmpeg_command[ffmpeg_command.index("-codec") + 1] == "copy"
    assert "libx264" not in ffmpeg_command
    assert "+faststart" in ffmpeg_command


@pytest.mark.asyncio
async def test_streaming_ready_mp4_skips_ffmpeg(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00\x00\x00\x08ftyp\x00\x00\x00\x08moov")

    class ProbeProcess:
        returncode = 0

        async def communicate(self):
            payload = {
                "format": {"format_name": "mov,mp4"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
            return json.dumps(payload).encode(), b""

    async def probe_only(*args, **_kwargs):
        assert args[0] == "ffprobe"
        return ProbeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", probe_only)
    assert await _transcode_video(source, Cancellation()) == source


@pytest.mark.asyncio
async def test_compatible_video_only_transcodes_audio(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.webm"
    source.write_bytes(b"video")
    commands: list[tuple[object, ...]] = []

    class ProbeProcess:
        returncode = 0

        async def communicate(self):
            payload = {
                "format": {"format_name": "matroska,webm"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                    },
                    {"codec_type": "audio", "codec_name": "opus"},
                ],
            }
            return json.dumps(payload).encode(), b""

    class FfmpegProcess:
        returncode = None
        stderr = None

        async def wait(self):
            Path(commands[-1][-1]).write_bytes(b"prepared")
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = -15

    async def create(*args, **_kwargs):
        commands.append(args)
        return ProbeProcess() if args[0] == "ffprobe" else FfmpegProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    await _transcode_video(source, Cancellation())
    command = commands[-1]
    assert command[command.index("-codec:v") + 1] == "copy"
    assert command[command.index("-codec:a") + 1] == "aac"
    assert "libx264" not in command


@pytest.mark.asyncio
async def test_video_transcode_rejects_non_streaming_pixel_format(
    monkeypatch, tmp_path
) -> None:
    class ProbeProcess:
        returncode = 0

        async def communicate(self):
            return (
                json.dumps(
                    {
                        "format": {"format_name": "mov,mp4"},
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "pix_fmt": "yuv444p",
                            },
                            {"codec_type": "audio", "codec_name": "aac"},
                        ],
                    }
                ).encode(),
                b"",
            )

    async def create(*_args, **_kwargs):
        return ProbeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    assert not await _is_telegram_compatible_video(tmp_path / "source.mp4")


def test_database_mapping_round_trip_and_schema() -> None:
    job = Job(
        "job",
        1,
        2,
        "https://example.com",
        "key",
        source_message_id=3,
        business_connection_id="business",
        inline_message_id="inline",
        audio_only=True,
    )
    values = _job_values(job)
    mapped = _job(JobRow(**values))
    assert mapped == job
    assert set(Base.metadata.tables) == {
        "app_users",
        "user_preferences",
        "download_jobs",
        "download_selections",
        "job_outbox",
        "app_analytics",
        "media_cache_v2",
        "spotify_credentials",
        "bot_admins",
        "invite_codes",
        "access_grants",
        "invite_redemptions",
    }
    preferences = _preferences(
        PreferencesRow(
            user_id=1,
            quality="720",
            audio_format="opus",
            captions=False,
            document_mode=True,
            show_buttons=False,
            delete_source=True,
            default_audio_only=False,
            compact_progress=True,
            youtube_mode="ask",
        )
    )
    assert _decode_preferences(json.dumps({"quality": "720", "unknown": 1})) .quality == "720"
    assert preferences.document_mode
    assert preferences.compact_progress
    assert preferences.youtube_mode == "ask"
    ask_preferences = _preferences(
        PreferencesRow(user_id=2, audio_format="opus", youtube_mode="ask")
    )
    assert ask_preferences.youtube_mode == "ask"
    assert ask_preferences.audio_format == "opus"
    now = datetime.now(UTC)
    selection = SelectionRequest(
        token="selection",
        user_id=1,
        chat_id=2,
        urls=("https://example.com/a", "https://example.com/b"),
        platforms=(Platform.YOUTUBE, Platform.GENERIC),
        mode=SelectionMode.VIDEO,
        quality="720",
        delivery=DeliveryMode.FILE,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status_message_id=4,
    )
    assert _selection(SelectionRow(**_selection_values(selection))) == selection
    assert _decode_selection_urls('["https://example.com/legacy"]') == (
        ("https://example.com/legacy",),
        PlaylistScope.NONE,
    )
    encoded_urls = _encode_selection_urls(
        ("https://youtube.com/playlist?list=PL123",), PlaylistScope.PLAYLIST
    )
    assert _decode_selection_urls(encoded_urls) == (
        ("https://youtube.com/playlist?list=PL123",),
        PlaylistScope.PLAYLIST,
    )
    manifest = json.dumps(
        [
            {
                "path": "/tmp/x.m4a",
                "kind": "audio",
                "size": 1,
                "checksum": "x",
                "mime_type": "audio/mp4",
                "title": "Track",
                "author": "Artist",
                "duration_ms": 12_000,
            }
        ]
    )
    decoded = _decode_artifacts(manifest)[0]
    assert decoded.kind is MediaKind.AUDIO
    assert (decoded.title, decoded.author, decoded.duration_ms) == (
        "Track",
        "Artist",
        12_000,
    )


@pytest.mark.asyncio
async def test_selection_repository_rejects_invalid_playlist_scope_type() -> None:
    repository = SqlSelectionRepository(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid playlist scope"):
        await repository.update(
            "selection", 1, datetime.now(UTC), playlist_scope=object()
        )


class FakeRedis:
    def __init__(self) -> None:
        self.groups = []
        self.added = []
        self.acked = []
        self.reads = []
        self.claimed = ("0-0", [])

    async def xgroup_create(self, *args, **kwargs):
        self.groups.append((args, kwargs))

    async def xadd(self, stream, fields, **kwargs):
        self.added.append((stream, fields, kwargs))
        return b"1-0"

    async def xreadgroup(self, *_args, **_kwargs):
        return self.reads

    async def xack(self, *args):
        self.acked.append(args)

    async def xautoclaim(self, *_args, **_kwargs):
        return self.claimed


@pytest.mark.asyncio
async def test_redis_stream_queue_and_progress_contracts() -> None:
    redis = FakeRedis()
    queue = RedisJobQueue(redis)
    await queue.initialize()
    assert await queue.publish("job") == "1-0"
    redis.reads = [(b"downloads", [(b"2-0", {b"job_id": b"job"})])]
    assert [item async for item in queue.consume("worker")] == [("2-0", "job")]
    await queue.ack("2-0")
    redis.claimed = ("0-0", [(b"3-0", {b"job_id": b"lost"})])
    assert await queue.reclaim("worker", idle_ms=10) == (("3-0", "lost"),)

    bus = RedisProgressBus(redis)
    await bus.initialize()
    progress = Progress(
        "job",
        JobStage.DOWNLOADING,
        42,
        downloaded_bytes=420,
        total_bytes=1000,
        total_bytes_is_estimate=True,
        elapsed_seconds=3,
        indeterminate=True,
    )
    await bus.publish(progress)
    payload = redis.added[-1][1]["payload"]
    redis.reads = [(b"download-progress", [(b"4-0", {b"payload": payload.encode()})])]
    values = [item async for item in bus.consume("bot")]
    assert values[0].percent == 42
    assert values[0].total_bytes_is_estimate
    assert values[0].elapsed_seconds == 3
    assert values[0].indeterminate
    assert redis.acked[-1][-1] == b"4-0"
    assert _text(b"value") == "value" and _text(1) == "1"
