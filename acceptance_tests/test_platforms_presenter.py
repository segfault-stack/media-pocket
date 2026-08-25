from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from downloader_bot.adapters.telegram.presenter import (
    render_progress,
    render_selection,
    selection_keyboard,
    settings_home_text,
    settings_page,
)
from downloader_bot.domain import (
    DeliveryMode,
    ErrorCode,
    JobStage,
    MediaKind,
    Platform,
    Progress,
    SelectionMode,
    SelectionRequest,
    UserPreferences,
)
from downloader_bot.domain.errors import DownloadError
from downloader_bot.domain.models import MediaSource
from downloader_bot.infrastructure.platforms import (
    PLATFORM_DOMAINS,
    DefaultPlatformRegistry,
    HitMozPlatformAdapter,
    SpotifyPlatformAdapter,
    YtDlpPlatformAdapter,
    ZaycevPlatformAdapter,
)


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://youtu.be/x", Platform.YOUTUBE),
        ("https://vm.tiktok.com/x", Platform.TIKTOK),
        ("https://instagram.com/p/x", Platform.INSTAGRAM),
        ("https://x.com/u/status/1", Platform.X),
        ("https://pin.it/x", Platform.PINTEREST),
        ("https://threads.net/@u/post/x", Platform.THREADS),
        ("https://soundcloud.com/u/x", Platform.SOUNDCLOUD),
        ("https://open.spotify.com/track/x", Platform.SPOTIFY),
        ("https://eu.hitmoz.com/album/532028", Platform.HITMOZ),
        ("https://hitmoz.me/album/532028", Platform.HITMOZ),
        ("https://hitmos.me/album/532028", Platform.HITMOZ),
        ("https://zaycev.net/pages/249134/24913430.shtml", Platform.ZAYCEV),
        ("https://example.com/file.mp4", Platform.GENERIC),
    ],
)
def test_registry_detects_every_supported_platform(url, platform) -> None:
    assert DefaultPlatformRegistry().detect(url).platform is platform


def test_platform_inventory_is_complete() -> None:
    assert set(PLATFORM_DOMAINS) == set(Platform) - {Platform.GENERIC}


def test_registry_routes_fixed_provider_cookie_files() -> None:
    registry = DefaultPlatformRegistry(
        cookies_file="/cookies/cookies.txt",
        cookies_files={
            Platform.YOUTUBE: "/cookies/youtube.txt",
            Platform.TIKTOK: "/cookies/tiktok.txt",
            Platform.INSTAGRAM: "/cookies/instagram.txt",
            Platform.X: "/cookies/x.txt",
        },
    )
    assert registry.detect("https://youtube.com/watch?v=x").cookies_file == (
        "/cookies/youtube.txt"
    )
    assert registry.detect("https://vm.tiktok.com/x").cookies_file == (
        "/cookies/tiktok.txt"
    )
    assert registry.detect("https://instagram.com/p/x").cookies_file == (
        "/cookies/instagram.txt"
    )
    assert registry.detect("https://twitter.com/u/status/1").cookies_file == (
        "/cookies/x.txt"
    )
    assert registry.detect("https://open.spotify.com/track/x").cookies_file == (
        "/cookies/youtube.txt"
    )
    assert registry.detect("https://soundcloud.com/u/x").cookies_file == (
        "/cookies/cookies.txt"
    )


@pytest.mark.asyncio
async def test_registry_uses_specialized_adapters() -> None:
    registry = DefaultPlatformRegistry()
    assert isinstance(
        registry.detect("https://youtube.com/watch?v=x"), YtDlpPlatformAdapter
    )
    assert isinstance(
        registry.detect("https://example.com/file.mp4"), YtDlpPlatformAdapter
    )
    assert isinstance(
        registry.detect("https://open.spotify.com/track/x"), SpotifyPlatformAdapter
    )

    client = httpx.AsyncClient()
    registry = DefaultPlatformRegistry(client=client)
    assert isinstance(
        registry.detect("https://zaycev.net/pages/249134/24913430.shtml"),
        ZaycevPlatformAdapter,
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_ytdlp_fixture_maps_album_assets_and_photos(monkeypatch) -> None:
    payload = {
        "title": "album",
        "entries": [
            {"url": "https://cdn/x.jpg", "ext": "jpg"},
            {"url": "https://cdn/y.mp4", "ext": "mp4", "vcodec": "h264"},
        ],
    }

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps(payload).encode(), b""

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    post = await YtDlpPlatformAdapter(Platform.INSTAGRAM).resolve(
        "https://instagram.com/p/x", UserPreferences(quality="720")
    )
    assert [asset.kind for asset in post.assets] == [MediaKind.PHOTO, MediaKind.VIDEO]


@pytest.mark.asyncio
async def test_ytdlp_passes_configured_po_token_provider(monkeypatch) -> None:
    command = ()

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"url":"https://cdn/video.mp4","ext":"mp4"}', b""

    async def create(*args, **_kwargs):
        nonlocal command
        command = args
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    await YtDlpPlatformAdapter(
        Platform.YOUTUBE,
        youtube_pot_provider_url="http://youtube-pot-provider:4416",
    ).resolve("https://youtube.com/watch?v=x", UserPreferences())

    extractor_args = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--extractor-args"
    ]
    assert extractor_args == [
        "youtube:player_client=mweb",
        "youtubepot-bgutilhttp:base_url=http://youtube-pot-provider:4416",
    ]


@pytest.mark.asyncio
async def test_ytdlp_preserves_provider_headers_cookies_and_media_metadata(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://cdn.tiktok.example/video.mp4",
        "ext": "mp4",
        "vcodec": "h264",
        "title": "TikTok title",
        "uploader": "creator",
        "duration": 12.5,
        "thumbnail": "https://cdn.tiktok.example/cover.jpg",
        "requested_downloads": [
            {
                "url": "https://cdn.tiktok.example/video.mp4",
                "http_headers": {"Referer": "https://www.tiktok.com/@creator/video/1"},
                "cookies": (
                    "session=secret; Domain=.tiktok.com; Path=/; Secure; "
                    "csrf=token; Domain=.tiktok.com; Path=/; Secure"
                ),
            }
        ],
    }

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps(payload).encode(), b""

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    post = await YtDlpPlatformAdapter(Platform.TIKTOK).resolve(
        "https://vm.tiktok.com/example", UserPreferences()
    )
    asset = post.assets[0]
    headers = dict(asset.request_headers)
    assert headers["Referer"].startswith("https://www.tiktok.com/")
    assert headers["Cookie"] == "session=secret; csrf=token"
    assert asset.author == "creator"
    assert asset.duration_ms == 12_500
    assert asset.thumbnail_url == "https://cdn.tiktok.example/cover.jpg"


@pytest.mark.asyncio
async def test_ytdlp_audio_selection_uses_requested_download_codec(monkeypatch) -> None:
    payload = {
        "url": "https://cdn.youtube.example/page-default.mp4",
        "ext": "mp4",
        "vcodec": "avc1",
        "title": "Ricky Martin - Livin la vida Loca (DnB Remix)",
        "uploader": "MassiveMasu",
        "requested_downloads": [
            {
                "url": "https://cdn.youtube.example/audio.m4a",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
            }
        ],
    }

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps(payload).encode(), b""

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    post = await YtDlpPlatformAdapter(Platform.YOUTUBE).resolve(
        "https://youtube.com/watch?v=x", UserPreferences(), audio_only=True
    )
    asset = post.assets[0]
    assert asset.kind is MediaKind.AUDIO
    assert asset.title == "Livin la vida Loca (DnB Remix)"
    assert asset.author == "Ricky Martin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "code", "retryable"),
    [
        ("This video is private", ErrorCode.PRIVATE, False),
        ("HTTP Error 429: rate limit", ErrorCode.RATE_LIMITED, True),
        ("extractor failed", ErrorCode.UNAVAILABLE, False),
    ],
)
async def test_ytdlp_maps_provider_failures(
    monkeypatch, detail, code, retryable
) -> None:
    class Process:
        returncode = 1

        async def communicate(self):
            return b"", detail.encode()

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    with pytest.raises(DownloadError) as raised:
        await YtDlpPlatformAdapter(
            Platform.TIKTOK, cookies_file="cookies/tiktok.txt"
        ).resolve("https://vm.tiktok.com/example", UserPreferences())
    assert raised.value.code is code
    assert raised.value.retryable is retryable


def test_ytdlp_asset_fallbacks_and_rejects_missing_media() -> None:
    asset = YtDlpPlatformAdapter._asset(
        {
            "requested_formats": [{"url": "https://cdn.example/audio.m4a"}],
            "vcodec": "none",
            "artists": [{"name": "One"}, "Two"],
            "duration": -1,
            "http_headers": {"X-Test": "yes"},
        },
        2,
    )
    assert asset.kind is MediaKind.AUDIO
    assert asset.author == "One, Two"
    assert asset.duration_ms is None
    assert dict(asset.request_headers)["X-Test"] == "yes"
    with pytest.raises(DownloadError, match="no media URL"):
        YtDlpPlatformAdapter._asset({}, 0)


@pytest.mark.asyncio
async def test_spotify_fixture_resolves_to_audio(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"title": "Track", "author_name": "Artist"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = SpotifyPlatformAdapter(Platform.SPOTIFY, client=client)

        async def resolved(*_args, **_kwargs):
            from downloader_bot.domain import MediaAsset, MediaPost

            return MediaPost(
                "source",
                Platform.SPOTIFY,
                (MediaAsset("https://cdn/x.m4a", MediaKind.VIDEO),),
            )

        monkeypatch.setattr(SpotifyPlatformAdapter, "_resolve_target", resolved)
        post = await adapter.resolve(
            "https://open.spotify.com/track/x", UserPreferences()
        )
    assert post.assets[0].kind is MediaKind.AUDIO
    assert post.assets[0].title == "Track"
    assert post.assets[0].author == "Artist"


@pytest.mark.asyncio
async def test_spotify_track_prefers_exact_catalog_metadata(monkeypatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oembed":
            return httpx.Response(200, json={"title": "Opaque oEmbed title"})
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(
            200,
            json={
                "name": "Exact Track",
                "artists": [
                    {"name": "Artist"},
                    {"name": "Guest"},
                    {"name": "artist"},
                ],
                "duration_ms": 123_456,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = SpotifyPlatformAdapter(
            Platform.SPOTIFY,
            client=client,
            client_id="id",
            client_secret="secret",
        )

        async def resolved(_self, target, **_kwargs):
            from downloader_bot.domain import MediaAsset, MediaPost

            assert target == "ytsearch1:Artist, Guest - Exact Track"
            return MediaPost(
                target,
                Platform.SPOTIFY,
                (MediaAsset("https://cdn/x.m4a", MediaKind.AUDIO),),
            )

        monkeypatch.setattr(SpotifyPlatformAdapter, "_resolve_target", resolved)
        post = await adapter.resolve(
            "https://open.spotify.com/track/catalog-id", UserPreferences()
        )
    asset = post.assets[0]
    assert (asset.title, asset.author, asset.duration_ms) == (
        "Exact Track",
        "Artist, Guest",
        123_456,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ("token_status", "token_body", "track_status", "track_body")
)
async def test_spotify_track_catalog_failures_fall_back_to_oembed(failure) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            if failure == "token_status":
                return httpx.Response(500)
            if failure == "token_body":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"access_token": "token"})
        if failure == "track_status":
            return httpx.Response(404)
        if failure == "track_body":
            return httpx.Response(200, json={})
        raise AssertionError("unexpected request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        metadata = await SpotifyPlatformAdapter(
            Platform.SPOTIFY,
            client=client,
            client_id="id",
            client_secret="secret",
        )._track_metadata("x")
    assert metadata is None


@pytest.mark.asyncio
async def test_spotify_native_track_keeps_youtube_fallback_query(monkeypatch) -> None:
    class Process:
        returncode = 0

        async def communicate(self):
            return (
                b'{"type":"track","identifier":"spotify:track:abc","title":"Track","author":"Artist","durationMs":123000}',
                b"",
            )

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    post = await SpotifyPlatformAdapter(
        Platform.SPOTIFY, command="spotify-streamer"
    ).resolve("https://open.spotify.com/track/x", UserPreferences())
    assert post.assets[0].source is MediaSource.SPOTIFY_STREAM
    assert post.assets[0].fallback_query == "Artist - Track"
    assert post.assets[0].author == "Artist"
    assert post.assets[0].duration_ms == 123000


@pytest.mark.asyncio
async def test_spotify_album_expands_every_track(monkeypatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "One",
                        "artists": [{"name": "Artist"}],
                        "duration_ms": 10_000,
                    },
                    {"name": "Two", "artists": [{"name": "Artist"}]},
                ],
                "next": None,
            },
        )

    transport = httpx.MockTransport(respond)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = SpotifyPlatformAdapter(
            Platform.SPOTIFY,
            client=client,
            client_id="id",
            client_secret="secret",
        )

        async def resolved(_self, target, **_kwargs):
            from downloader_bot.domain import MediaAsset, MediaPost

            return MediaPost(
                target,
                Platform.SPOTIFY,
                (MediaAsset("https://cdn/x.m4a", MediaKind.AUDIO),),
            )

        monkeypatch.setattr(SpotifyPlatformAdapter, "_resolve_target", resolved)
        post = await adapter.resolve(
            "https://open.spotify.com/album/album-id", UserPreferences()
        )
    assert len(post.assets) == 2
    assert [asset.index for asset in post.assets] == [0, 1]
    assert post.assets[0].title == "One"
    assert post.assets[0].author == "Artist"
    assert post.assets[0].duration_ms == 10_000


@pytest.mark.asyncio
async def test_spotify_reports_missing_resolvers_metadata_and_credentials() -> None:
    with pytest.raises(DownloadError, match="not configured"):
        await SpotifyPlatformAdapter(Platform.SPOTIFY).resolve(
            "https://open.spotify.com/track/x", UserPreferences()
        )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    ) as client:
        adapter = SpotifyPlatformAdapter(Platform.SPOTIFY, client=client)
        with pytest.raises(DownloadError, match="metadata is unavailable"):
            await adapter.resolve("https://open.spotify.com/track/x", UserPreferences())
        with pytest.raises(DownloadError, match="credentials are required"):
            await adapter.resolve("https://open.spotify.com/album/x", UserPreferences())


@pytest.mark.asyncio
async def test_spotify_collection_maps_playlist_items_and_rate_limits() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(429))
    ) as client:
        adapter = SpotifyPlatformAdapter(
            Platform.SPOTIFY, client=client, client_id="id", client_secret="secret"
        )
        with pytest.raises(DownloadError) as raised:
            await adapter._collection_tracks("album", "x")
        assert raised.value.code is ErrorCode.RATE_LIMITED
        assert raised.value.retryable is True

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(
            200,
            json={
                "items": [
                    {"track": {"name": "Song", "artists": [{"name": "Artist"}]}},
                    {"track": None},
                ],
                "next": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tracks = await SpotifyPlatformAdapter(
            Platform.SPOTIFY, client=client, client_id="id", client_secret="secret"
        )._collection_tracks("playlist", "x")
    assert tracks == ({"title": "Song", "author": "Artist", "duration_ms": None},)


@pytest.mark.asyncio
async def test_hitmoz_album_parser_preserves_track_order_and_deduplicates_links() -> (
    None
):
    page = """
        <html><head><title>Example Album | HitMoz</title></head><body>
        <a href="/get/music/20260822/First_Artist_-_First_Song_123456.mp3">first</a>
        <a href="/get/music/20260822/First_Artist_-_First_Song_123456.mp3">duplicate</a>
        <a href="https://eu.hitmoz.com/get/music/20260822/Second_Artist_-_Second_Song_654321.mp3">second</a>
        </body></html>
    """
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=page))
    async with httpx.AsyncClient(transport=transport) as client:
        post = await HitMozPlatformAdapter(Platform.HITMOZ, client).resolve(
            "https://eu.hitmoz.com/album/532028", UserPreferences()
        )
    assert post.title == "Example Album | HitMoz"
    assert [asset.kind for asset in post.assets] == [MediaKind.AUDIO, MediaKind.AUDIO]
    assert [asset.index for asset in post.assets] == [0, 1]
    assert post.assets[0].title == "First Song"
    assert post.assets[0].author == "First Artist"
    assert post.assets[1].source_url.endswith("Second_Artist_-_Second_Song_654321.mp3")


@pytest.mark.asyncio
async def test_hitmoz_uses_mirror_when_source_domain_is_unavailable() -> None:
    page = '<a href="/get/music/20260822/Artist_-_Song_123456.mp3">track</a>'

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hitmos.me":
            return httpx.Response(403)
        assert request.url.host == "eu.hitmoz.com"
        return httpx.Response(200, text=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        post = await HitMozPlatformAdapter(Platform.HITMOZ, client).resolve(
            "https://hitmos.me/album/532028", UserPreferences()
        )
    assert post.source_url == "https://hitmos.me/album/532028"
    assert post.assets[0].source_url.startswith("https://eu.hitmoz.com/")


@pytest.mark.asyncio
async def test_zaycev_resolves_track_to_direct_audio() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pages/track"):
            assert request.url.params["id"] == "24913430"
            return httpx.Response(
                200,
                json={
                    "info": {
                        "track": "Уу-хуу",
                        "artistName": "VACÍO, Пошлая Молли",
                        "durationTime": 138,
                        "size": 2.11,
                        "notAvailable": False,
                    }
                },
            )
        if request.url.path.endswith("/track/filezmeta"):
            assert json.loads(request.content) == {
                "trackIds": [24913430],
                "subscription": False,
            }
            return httpx.Response(
                200,
                json={"tracks": [{"id": 24913430, "download": "token"}]},
            )
        assert request.url.path.endswith("/track/download/token")
        return httpx.Response(200, text="https://cdn.example/track.mp3")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        post = await ZaycevPlatformAdapter(Platform.ZAYCEV, client).resolve(
            "https://zaycev.net/pages/249134/24913430.shtml", UserPreferences()
        )

    assert post.platform is Platform.ZAYCEV
    assert post.title == "Уу-хуу"
    assert post.author == "VACÍO, Пошлая Молли"
    assert post.assets[0].source_url == "https://cdn.example/track.mp3"
    assert post.assets[0].kind is MediaKind.AUDIO
    assert post.assets[0].duration_ms == 138_000
    assert post.assets[0].size_hint == 2_110_000


@pytest.mark.asyncio
async def test_zaycev_supports_streaming_tokens_and_rejects_bad_urls() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pages/track"):
            return httpx.Response(200, json={"info": {"track": "Song"}})
        if request.url.path.endswith("/track/filezmeta"):
            return httpx.Response(
                200, json={"tracks": [{"id": 24913430, "streaming": "token"}]}
            )
        return httpx.Response(200, json={"url": "https://cdn.example/song.mp3"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZaycevPlatformAdapter(Platform.ZAYCEV, client)
        post = await adapter.resolve(
            "https://zaycev.net/pages/249134/24913430.shtml", UserPreferences()
        )
        with pytest.raises(DownloadError) as raised:
            await adapter.resolve("https://zaycev.net/artist/249134", UserPreferences())
    assert post.assets[0].source_url == "https://cdn.example/song.mp3"
    assert raised.value.code is ErrorCode.UNSUPPORTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "metadata", "tracks", "code", "retryable"),
    [
        (200, {"notAvailable": True}, [], ErrorCode.UNAVAILABLE, False),
        (200, {}, [], ErrorCode.UNAVAILABLE, False),
        (429, {}, [], ErrorCode.RATE_LIMITED, True),
        (500, {}, [], ErrorCode.PROVIDER_FAILURE, True),
    ],
)
async def test_zaycev_maps_unavailable_and_http_failures(
    status, metadata, tracks, code, retryable
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pages/track"):
            return httpx.Response(status, json={"info": metadata})
        return httpx.Response(200, json={"tracks": tracks})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError) as raised:
            await ZaycevPlatformAdapter(Platform.ZAYCEV, client).resolve(
                "https://zaycev.net/pages/249134/24913430.shtml",
                UserPreferences(),
            )
    assert raised.value.code is code
    assert raised.value.retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "track_file",
    ({"id": 24913430}, {"id": 24913430, "download": "token"}),
)
async def test_zaycev_rejects_missing_or_invalid_media_url(track_file) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pages/track"):
            return httpx.Response(200, json={"info": {"track": "Song"}})
        if request.url.path.endswith("/track/filezmeta"):
            return httpx.Response(200, json={"tracks": [track_file]})
        return httpx.Response(200, text="not-a-url")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadError) as raised:
            await ZaycevPlatformAdapter(Platform.ZAYCEV, client).resolve(
                "https://zaycev.net/pages/249134/24913430.shtml",
                UserPreferences(),
            )
    assert raised.value.code is ErrorCode.UNAVAILABLE


@pytest.mark.parametrize("code", list(ErrorCode))
def test_presenter_has_one_safe_error_format(code) -> None:
    rendered = render_progress(
        Progress(
            "job", JobStage.FAILED, 100, error_code=code, detail="raw provider secret"
        )
    )
    assert "raw provider secret" not in rendered
    assert "Download failed" in rendered


@pytest.mark.parametrize(
    ("platform", "mode", "expected", "excluded"),
    [
        (Platform.YOUTUBE, SelectionMode.VIDEO, "🎬 Video", None),
        (Platform.TIKTOK, SelectionMode.MEDIA, "▶️ Media", "1080p"),
        (Platform.SPOTIFY, SelectionMode.AUDIO, "🎧 Audio", "▶️ Media"),
    ],
)
def test_provider_specific_selection_cards(platform, mode, expected, excluded) -> None:
    now = datetime.now(UTC)
    selection = SelectionRequest(
        token="12345678-1234-1234-1234-123456789012",
        user_id=1,
        chat_id=2,
        urls=("https://example.com/path?x=1&y=2",),
        platforms=(platform,),
        mode=mode,
        quality="1080",
        delivery=DeliveryMode.FILE
        if platform is Platform.SPOTIFY
        else DeliveryMode.MEDIA,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    text = render_selection(selection)
    labels = [
        button.text
        for row in selection_keyboard(selection).inline_keyboard
        for button in row
    ]
    assert expected in " ".join(labels)
    assert "&amp;" in text
    assert all(
        not button.callback_data or len(button.callback_data.encode()) <= 64
        for row in selection_keyboard(selection).inline_keyboard
        for button in row
    )
    if excluded:
        assert excluded not in labels


def test_audio_selection_hides_video_quality_and_uses_compact_actions() -> None:
    now = datetime.now(UTC)
    selection = SelectionRequest(
        token="12345678-1234-1234-1234-123456789012",
        user_id=1,
        chat_id=2,
        urls=("https://youtube.com/watch?v=x",),
        platforms=(Platform.YOUTUBE,),
        mode=SelectionMode.AUDIO,
        quality="best",
        delivery=DeliveryMode.MEDIA,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    text = render_selection(selection)
    keyboard = selection_keyboard(selection).inline_keyboard
    labels = [button.text for row in keyboard for button in row]
    assert "Best" not in text
    assert not any(label in labels for label in ("✓ Best", "1080p", "720p", "480p"))
    assert "🎧 Audio · ▶️ In chat" in text
    assert [button.text for button in keyboard[-1]] == ["⬇️ Download", "Cancel"]


@pytest.mark.parametrize(
    ("mode", "label"),
    (("video", "Video"), ("audio", "Audio"), ("ask", "Always ask")),
)
def test_download_settings_show_youtube_start_mode(mode, label) -> None:
    preferences = UserPreferences(youtube_mode=mode)
    text, keyboard = settings_page(preferences, "download")
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert f"YouTube: {label}" in text
    assert f"YouTube: {label}" in settings_home_text(preferences)
    selected = [button for button in buttons if button.text.startswith("✓ ")]
    assert any(button.callback_data == f"settings:youtube:{mode}" for button in selected)


def test_progress_renders_structured_metrics_and_batch_counter() -> None:
    rendered = render_progress(
        Progress(
            "job",
            JobStage.DOWNLOADING,
            64,
            item=2,
            item_count=5,
            downloaded_bytes=64 * 1024 * 1024,
            total_bytes=100 * 1024 * 1024,
            speed_bytes_per_second=8 * 1024 * 1024,
            eta_seconds=5,
        )
    )
    assert "64%" in rendered
    assert "2 of 5" in rendered
    assert "64.0 MB / 100.0 MB" in rendered
    assert "8.0 MB/s" in rendered
    assert "ETA 5s" in rendered
