from __future__ import annotations

import asyncio
import html
import json
import os
import re
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from http.cookies import SimpleCookie
from pathlib import Path
from typing import TypedDict
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from downloader_bot.application.ports import Cancellation, PlatformAdapter
from downloader_bot.domain import (
    MediaAsset,
    MediaChapter,
    MediaKind,
    MediaPost,
    MediaSource,
    Platform,
    UserPreferences,
)
from downloader_bot.domain.audio_names import (
    normalize_artist_names,
    normalize_audio_title,
    resolve_audio_title_artist,
    split_audio_artist_title,
)
from downloader_bot.domain.errors import DownloadError
from downloader_bot.domain.models import ErrorCode
from downloader_bot.infrastructure.cookies import writable_cookie_file

PLATFORM_DOMAINS: dict[Platform, tuple[str, ...]] = {
    Platform.YOUTUBE: ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    Platform.TIKTOK: ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    Platform.INSTAGRAM: ("instagram.com", "instagr.am"),
    Platform.X: ("x.com", "twitter.com", "t.co"),
    Platform.PINTEREST: ("pinterest.com", "pin.it"),
    Platform.THREADS: ("threads.net", "threads.com"),
    Platform.SOUNDCLOUD: ("soundcloud.com", "on.soundcloud.com"),
    Platform.SPOTIFY: ("open.spotify.com",),
    Platform.HITMOZ: (),
    Platform.ZAYCEV: ("zaycev.net",),
    Platform.TWO_CH: ("2ch.hk", "2ch.su", "2ch.life"),
}

DIRECT_VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogv",
        ".ts",
        ".webm",
        ".wmv",
    }
)
DIRECT_AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aiff",
        ".alac",
        ".flac",
        ".m4a",
        ".mp3",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }
)
_DIRECT_MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HITMOZ_DOWNLOAD_PATTERN = re.compile(
    r'''href\s*=\s*["'](?P<url>(?:https?:)?//[^"']*?/get/music/[^"']+?\.mp3(?:\?[^"']*)?|/get/music/[^"']+?\.mp3(?:\?[^"']*)?)["']''',
    re.IGNORECASE,
)
_HITMOZ_FILENAME_PATTERN = re.compile(r"_(?P<id>\d{6,})$")
_HTML_TITLE_PATTERN = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
_HITMOZ_MIRRORS = (
    "https://eu.hitmoz.com",
    "https://ru.hitmoz.org",
    "https://rus.hitmoz.org",
    "https://hitmos.me",
)
_ZAYCEV_TRACK_PATTERN = re.compile(r"^/pages/\d+/(?P<id>\d+)\.shtml$")
_ZAYCEV_API_BASE = "https://zaycev.net/api/external"
_PROCESS_POLL_SECONDS = 0.25
_PROCESS_STOP_SECONDS = 2.0
_MAX_DESCRIPTION_CHAPTERS = 100
_DESCRIPTION_TIMESTAMP_RE = re.compile(
    r"(?<!\d)(?:(?P<hours>\d{1,2}):)?(?P<minutes>\d{1,3}):"
    r"(?P<seconds>[0-5]\d)(?!\d)"
)


class _SpotifyTrack(TypedDict):
    title: str
    author: str | None
    duration_ms: int | None
    thumbnail_url: str | None


async def _communicate_ytdlp(
    process: asyncio.subprocess.Process,
    *,
    cancellation: Cancellation | None,
    timeout_seconds: int,
) -> tuple[bytes, bytes]:
    task = asyncio.create_task(process.communicate())
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _stop_process_group(process)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise DownloadError(
                    ErrorCode.UNAVAILABLE, "Provider metadata lookup timed out"
                )
            done, _ = await asyncio.wait(
                {task}, timeout=min(_PROCESS_POLL_SECONDS, remaining)
            )
            if task in done:
                return task.result()
            if cancellation is not None and await cancellation.requested():
                await _stop_process_group(process)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
    except asyncio.CancelledError:
        await _stop_process_group(process)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_STOP_SECONDS)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()


@dataclass(slots=True)
class YtDlpPlatformAdapter:
    platform: Platform
    cookies_file: str | None = None
    format_selector: str = "bestvideo*+bestaudio/best"
    youtube_pot_provider_url: str | None = None
    resolve_timeout_seconds: int = 120

    async def resolve(
        self,
        url: str,
        preferences: UserPreferences,
        *,
        audio_only: bool = False,
        cancellation: Cancellation | None = None,
    ) -> MediaPost:
        selector = _format_selector(preferences, audio_only=audio_only)
        return await self._resolve_target(
            url,
            source_url=url,
            format_selector=selector,
            format_sort=_format_sort(
                audio_only=audio_only,
                document_mode=preferences.document_mode,
            ),
            force_audio=audio_only,
            include_playlist=preferences.include_playlist,
            cancellation=cancellation,
        )

    async def _resolve_target(
        self,
        target: str,
        *,
        source_url: str,
        format_selector: str | None = None,
        format_sort: str | None = None,
        force_audio: bool = False,
        include_playlist: bool = False,
        cancellation: Cancellation | None = None,
    ) -> MediaPost:
        with writable_cookie_file(self.cookies_file) as cookie_file:
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--ignore-config",
                "--dump-single-json",
                "--no-warnings",
                "--format",
                format_selector or self.format_selector,
                "--format-sort",
                format_sort or _format_sort(audio_only=force_audio),
                "--yes-playlist" if include_playlist else "--no-playlist",
            ]
            if not force_audio:
                command.extend(("--merge-output-format", "mp4/mkv"))
            if cookie_file:
                command.extend(("--cookies", cookie_file))
            if self.platform in {Platform.YOUTUBE, Platform.SPOTIFY}:
                command.extend(("--extractor-args", "youtube:player_client=mweb"))
            if (
                self.platform in {Platform.YOUTUBE, Platform.SPOTIFY}
                and self.youtube_pot_provider_url
            ):
                command.extend(
                    (
                        "--extractor-args",
                        f"youtubepot-bgutilhttp:base_url={self.youtube_pot_provider_url}",
                    )
                )
            command.append(target)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await _communicate_ytdlp(
                process,
                cancellation=cancellation,
                timeout_seconds=self.resolve_timeout_seconds,
            )
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()
            lowered = detail.lower()
            if "private" in lowered or "login" in lowered:
                code = ErrorCode.PRIVATE
            elif "deleted" in lowered or "removed" in lowered:
                code = ErrorCode.DELETED
            elif "region" in lowered or "country" in lowered or "geo" in lowered:
                code = ErrorCode.REGION_RESTRICTED
            elif "expired" in lowered:
                code = ErrorCode.EXPIRED
            elif "429" in lowered or "rate limit" in lowered:
                code = ErrorCode.RATE_LIMITED
            else:
                code = ErrorCode.UNAVAILABLE
            raise DownloadError(
                code,
                detail or "Provider could not resolve this URL",
                retryable=code is ErrorCode.RATE_LIMITED,
            )
        data = json.loads(stdout)
        entries = tuple(data.get("entries") or (data,))
        assets = tuple(
            self._asset(
                entry,
                index,
                extractor_url=entry.get("webpage_url")
                or entry.get("original_url")
                or target,
                format_selector=format_selector or self.format_selector,
                format_sort=format_sort or _format_sort(audio_only=force_audio),
                cookies_file=self.cookies_file,
                force_audio=force_audio,
            )
            for index, entry in enumerate(entries)
            if entry
        )
        if not assets:
            raise DownloadError(ErrorCode.UNAVAILABLE, "No downloadable media found")
        chapter_source = entries[0] if len(entries) == 1 else data
        return MediaPost(
            source_url=source_url,
            platform=self.platform,
            assets=assets,
            title=data.get("title"),
            author=data.get("uploader") or data.get("artist"),
            caption=data.get("description"),
            chapters=_media_chapters(chapter_source),
        )

    @staticmethod
    def _asset(
        entry: dict,
        index: int,
        *,
        extractor_url: str | None = None,
        format_selector: str | None = None,
        format_sort: str | None = None,
        cookies_file: str | None = None,
        force_audio: bool = False,
    ) -> MediaAsset:
        media_url = entry.get("url")
        requested_downloads = entry.get("requested_downloads") or ()
        requested_formats = entry.get("requested_formats") or ()
        requested = requested_downloads or requested_formats
        requires_extractor_download = (
            len(requested_downloads) > 1 or len(requested_formats) > 1
        )
        if requested and not media_url:
            media_url = requested[0].get("url")
            if not media_url:
                requires_extractor_download = True
        if not media_url:
            media_url = entry.get("webpage_url") or entry.get("original_url")
        if not isinstance(media_url, str):
            raise DownloadError(ErrorCode.UNAVAILABLE, "Provider returned no media URL")
        selected = requested[0] if requested and isinstance(requested[0], dict) else entry
        extension = str(
            selected.get("ext")
            or entry.get("ext")
            or urlsplit(media_url).path.rsplit(".", 1)[-1]
        ).lower()
        if force_audio:
            kind = MediaKind.AUDIO
        elif extension in {"jpg", "jpeg", "png", "webp", "gif"}:
            kind = MediaKind.PHOTO
        elif selected.get("vcodec", entry.get("vcodec")) == "none" or extension in {
            "mp3",
            "m4a",
            "aac",
            "opus",
            "ogg",
            "wav",
            "flac",
        }:
            kind = MediaKind.AUDIO
        else:
            kind = MediaKind.VIDEO
        title = entry.get("title")
        author = _media_author(entry)
        if kind is MediaKind.AUDIO:
            title, author = resolve_audio_title_artist(entry)
        return MediaAsset(
            source_url=media_url,
            kind=kind,
            index=index,
            title=title if isinstance(title, str) else None,
            author=author,
            duration_ms=_duration_ms(entry.get("duration")),
            size_hint=entry.get("filesize") or entry.get("filesize_approx"),
            request_headers=_request_headers(entry, selected),
            extractor_url=extractor_url,
            format_selector=format_selector,
            format_sort=format_sort,
            cookies_file=cookies_file,
            thumbnail_url=entry.get("thumbnail")
            if isinstance(entry.get("thumbnail"), str)
            else None,
            requires_extractor_download=requires_extractor_download,
        )


@dataclass(slots=True)
class DirectMediaPlatformAdapter:
    platform: Platform

    async def resolve(
        self,
        url: str,
        preferences: UserPreferences,
        *,
        audio_only: bool = False,
        cancellation: Cancellation | None = None,
    ) -> MediaPost:
        del preferences, audio_only, cancellation
        kind = _direct_media_kind(url)
        if kind is None:
            raise DownloadError(ErrorCode.UNSUPPORTED, "Unsupported direct media URL")
        parsed = urlsplit(url)
        title = parsed.path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or None
        asset = MediaAsset(
            source_url=url,
            kind=kind,
            title=title,
            request_headers=(
                ("Accept", f"{kind.value}/*,application/octet-stream;q=0.9,*/*;q=0.8"),
                ("Referer", f"{parsed.scheme}://{parsed.netloc}/"),
                ("User-Agent", _DIRECT_MEDIA_USER_AGENT),
            ),
        )
        return MediaPost(
            source_url=url,
            platform=self.platform,
            assets=(asset,),
            title=title,
        )


@dataclass(slots=True)
class TwoChPlatformAdapter(DirectMediaPlatformAdapter):
    platform: Platform = Platform.TWO_CH

    async def resolve(
        self,
        url: str,
        preferences: UserPreferences,
        *,
        audio_only: bool = False,
        cancellation: Cancellation | None = None,
    ) -> MediaPost:
        if not _is_direct_video_url(url):
            raise DownloadError(ErrorCode.UNSUPPORTED, "Unsupported 2ch video URL")
        post = await super().resolve(
            url,
            preferences,
            audio_only=audio_only,
            cancellation=cancellation,
        )
        candidates = _two_ch_media_urls(url)
        return replace(
            post,
            assets=(
                replace(
                    post.assets[0],
                    source_url=candidates[0],
                    fallback_urls=candidates[1:],
                ),
            ),
        )


@dataclass(slots=True)
class SpotifyPlatformAdapter(YtDlpPlatformAdapter):
    client: httpx.AsyncClient | None = None
    client_id: str | None = None
    client_secret: str | None = None
    market: str = "US"
    command: str | None = None
    cache_dir: str = "spotify"
    resolve_timeout_seconds: int = 120

    async def resolve(
        self,
        url: str,
        preferences: UserPreferences,
        *,
        audio_only: bool = False,
        cancellation: Cancellation | None = None,
    ) -> MediaPost:
        if self.command:
            try:
                return await self._resolve_native(url)
            except (DownloadError, OSError, TimeoutError, json.JSONDecodeError):
                # Native Spotify is deliberately best-effort. The normal resolver below
                # keeps links usable when no Premium session is configured or it expires.
                pass
        if self.client is None:
            raise DownloadError(
                ErrorCode.PROVIDER_FAILURE, "Spotify resolver is not configured"
            )
        path = [part for part in urlsplit(url).path.split("/") if part]
        resource = path[0] if path else "track"
        resource_id = path[1] if len(path) > 1 else ""
        if resource in {"album", "playlist"}:
            if not self.client_id or not self.client_secret or not resource_id:
                raise DownloadError(
                    ErrorCode.PROVIDER_FAILURE,
                    "Spotify credentials are required for albums and playlists",
                )
            tracks = await self._collection_tracks(resource, resource_id)
            assets: list[MediaAsset] = []
            for index, track in enumerate(tracks):
                title = normalize_audio_title(track["title"], track["author"])
                query = " - ".join(
                    part
                    for part in (track["author"], title)
                    if isinstance(part, str) and part
                )
                resolved = await self._resolve_target(
                    f"ytsearch1:{query}",
                    source_url=url,
                    format_selector="bestaudio/best",
                    force_audio=True,
                    cancellation=cancellation,
                )
                if resolved.assets:
                    assets.append(
                        replace(
                            resolved.assets[0],
                            index=index,
                            kind=MediaKind.AUDIO,
                            title=title,
                            author=track["author"],
                            duration_ms=track["duration_ms"],
                            thumbnail_url=track["thumbnail_url"],
                        )
                    )
            if not assets:
                raise DownloadError(ErrorCode.UNAVAILABLE, "Spotify collection is empty")
            return MediaPost(
                url,
                Platform.SPOTIFY,
                tuple(assets),
                title=normalize_audio_title(tracks[0]["title"], tracks[0]["author"]),
            )
        response = await self.client.get(
            "https://open.spotify.com/oembed", params={"url": url}
        )
        response.raise_for_status()
        metadata = response.json()
        catalog = (
            await self._track_metadata(resource_id)
            if self.client_id and self.client_secret and resource_id
            else None
        )
        title = catalog["title"] if catalog else metadata.get("title")
        if not title:
            raise DownloadError(
                ErrorCode.UNAVAILABLE, "Spotify metadata is unavailable"
            )
        author = catalog["author"] if catalog else _spotify_oembed_author(metadata)
        thumbnail_url = (
            catalog["thumbnail_url"] if catalog else metadata.get("thumbnail_url")
        )
        if not isinstance(thumbnail_url, str):
            thumbnail_url = None
        normalized_title = normalize_audio_title(title, author)
        query = " - ".join(part for part in (author, normalized_title) if part)
        resolved = await self._resolve_target(
            f"ytsearch1:{query}",
            source_url=url,
            format_selector="bestaudio/best",
            force_audio=True,
            cancellation=cancellation,
        )
        return replace(
            resolved,
            platform=Platform.SPOTIFY,
            assets=tuple(
                replace(
                    asset,
                    kind=MediaKind.AUDIO,
                    title=normalized_title,
                    author=author,
                    duration_ms=catalog["duration_ms"] if catalog else None,
                    thumbnail_url=thumbnail_url,
                )
                for asset in resolved.assets
            ),
            title=normalized_title,
            author=author,
        )

    async def _track_metadata(self, resource_id: str) -> _SpotifyTrack | None:
        assert self.client is not None
        try:
            token_response = await self.client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(self.client_id or "", self.client_secret or ""),
            )
            if token_response.status_code >= 400:
                return None
            token = token_response.json().get("access_token")
            if not isinstance(token, str) or not token:
                return None
            response = await self.client.get(
                f"https://api.spotify.com/v1/tracks/{resource_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"market": self.market},
            )
            if response.status_code >= 400:
                return None
            track = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        title = track.get("name")
        if not isinstance(title, str) or not title:
            return None
        duration_ms = track.get("duration_ms")
        return {
            "title": title,
            "author": normalize_artist_names(track.get("artists")),
            "duration_ms": duration_ms if isinstance(duration_ms, int) else None,
            "thumbnail_url": _spotify_image_url(track.get("album")),
        }

    async def _resolve_native(self, url: str) -> MediaPost:
        assert self.command is not None
        process = await asyncio.create_subprocess_exec(
            self.command,
            "resolve",
            "--cache",
            self.cache_dir,
            "--uri",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.resolve_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DownloadError(
                ErrorCode.PROVIDER_FAILURE, "Spotify resolver timed out"
            ) from exc
        if process.returncode:
            raise DownloadError(
                ErrorCode.PROVIDER_FAILURE,
                stderr.decode(errors="replace").strip()[-1000:] or "Spotify resolver failed",
            )
        payload = json.loads(stdout)
        kind = payload.get("type")
        if kind == "track":
            asset = _native_spotify_asset(payload, 0)
            return MediaPost(url, Platform.SPOTIFY, (asset,), title=asset.title)
        if kind not in {"album", "playlist"}:
            raise DownloadError(ErrorCode.UNAVAILABLE, "Spotify returned an unsupported item")
        assets = tuple(
            _native_spotify_asset(track, index)
            for index, track in enumerate(payload.get("tracks", []))
        )
        if not assets:
            raise DownloadError(ErrorCode.UNAVAILABLE, "Spotify collection is empty")
        return MediaPost(url, Platform.SPOTIFY, assets, title=payload.get("name"))

    async def _collection_tracks(
        self, resource: str, resource_id: str
    ) -> tuple[_SpotifyTrack, ...]:
        assert self.client is not None
        token_response = await self.client.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id or "", self.client_secret or ""),
        )
        if token_response.status_code == 429:
            raise DownloadError(
                ErrorCode.RATE_LIMITED, "Spotify rate limit exceeded", retryable=True
            )
        token_response.raise_for_status()
        token = token_response.json()["access_token"]
        endpoint = (
            f"https://api.spotify.com/v1/albums/{resource_id}"
            if resource == "album"
            else f"https://api.spotify.com/v1/playlists/{resource_id}/tracks"
        )
        tracks: list[_SpotifyTrack] = []
        collection_thumbnail_url: str | None = None
        while endpoint:
            response = await self.client.get(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                params={"market": self.market, "limit": 50},
            )
            response.raise_for_status()
            payload = response.json()
            if resource == "album" and "tracks" in payload:
                collection_thumbnail_url = _spotify_image_url(payload)
                page = payload.get("tracks") or {}
            else:
                page = payload
            for raw in page.get("items", []):
                track = raw.get("track") if resource == "playlist" else raw
                if not isinstance(track, dict) or not track.get("name"):
                    continue
                artists = normalize_artist_names(track.get("artists"))
                duration_ms = track.get("duration_ms")
                tracks.append(
                    {
                        "title": str(track["name"]),
                        "author": artists,
                        "duration_ms": duration_ms
                        if isinstance(duration_ms, int)
                        else None,
                        "thumbnail_url": _spotify_image_url(track.get("album"))
                        or collection_thumbnail_url,
                    }
                )
            endpoint = page.get("next") or ""
        return tuple(tracks)


@dataclass(slots=True)
class HitMozPlatformAdapter:
    """Resolve HitMoz album and song pages to their direct MP3 resources."""

    platform: Platform
    client: httpx.AsyncClient

    async def resolve(
        self,
        url: str,
        preferences: UserPreferences,
        *,
        audio_only: bool = False,
        cancellation: Cancellation | None = None,
    ) -> MediaPost:
        del preferences, audio_only, cancellation
        last_error: DownloadError | None = None
        for candidate in _hitmoz_candidates(url):
            try:
                response = await self.client.get(
                    candidate,
                    headers={
                        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                        "Referer": f"{urlsplit(candidate).scheme}://{urlsplit(candidate).netloc}/",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                    },
                    follow_redirects=True,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_error = _hitmoz_http_error(exc)
                continue
            except httpx.HTTPError:
                last_error = DownloadError(
                    ErrorCode.PROVIDER_FAILURE, "Could not reach HitMoz", retryable=True
                )
                continue
            links = _hitmoz_download_links(response.text, str(response.url))
            if not links:
                last_error = DownloadError(
                    ErrorCode.UNAVAILABLE, "No downloadable tracks found on HitMoz"
                )
                continue
            assets = tuple(
                _hitmoz_asset(link, index) for index, link in enumerate(links)
            )
            return MediaPost(
                source_url=url,
                platform=self.platform,
                assets=assets,
                title=_html_title(response.text),
            )
        raise last_error or DownloadError(ErrorCode.UNAVAILABLE, "HitMoz album is unavailable")


@dataclass(slots=True)
class ZaycevPlatformAdapter:
    """Resolve a Zaycev.net track page through the site's public web API."""

    platform: Platform
    client: httpx.AsyncClient

    async def resolve(
        self,
        url: str,
        preferences: UserPreferences,
        *,
        audio_only: bool = False,
        cancellation: Cancellation | None = None,
    ) -> MediaPost:
        del preferences, audio_only, cancellation
        match = _ZAYCEV_TRACK_PATTERN.fullmatch(urlsplit(url).path)
        if not match:
            raise DownloadError(ErrorCode.UNSUPPORTED, "Unsupported Zaycev.net URL")
        track_id = match.group("id")
        headers = {
            "Origin": "https://zaycev.net",
            "Referer": url,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        try:
            metadata_response = await self.client.get(
                f"{_ZAYCEV_API_BASE}/pages/track",
                params={"id": track_id},
                headers=headers,
            )
            metadata_response.raise_for_status()
            metadata = metadata_response.json().get("info") or {}
            if metadata.get("notAvailable"):
                raise DownloadError(ErrorCode.UNAVAILABLE, "Zaycev.net track is unavailable")

            files_response = await self.client.post(
                f"{_ZAYCEV_API_BASE}/track/filezmeta",
                json={"trackIds": [int(track_id)], "subscription": False},
                headers=headers,
            )
            files_response.raise_for_status()
            track_file = next(
                (
                    item
                    for item in files_response.json().get("tracks") or ()
                    if str(item.get("id")) == track_id
                ),
                None,
            )
            if not track_file:
                raise DownloadError(ErrorCode.UNAVAILABLE, "Zaycev.net returned no media")

            media_url = await self._media_url(track_id, track_file, headers)
        except DownloadError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise DownloadError(
                    ErrorCode.RATE_LIMITED,
                    "Zaycev.net rate limit exceeded",
                    retryable=True,
                ) from exc
            raise DownloadError(
                ErrorCode.PROVIDER_FAILURE,
                f"Zaycev.net returned HTTP {exc.response.status_code}",
                retryable=exc.response.status_code >= 500,
            ) from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise DownloadError(
                ErrorCode.PROVIDER_FAILURE,
                "Could not resolve Zaycev.net track",
                retryable=isinstance(exc, httpx.HTTPError),
            ) from exc

        artist = normalize_artist_names(metadata.get("artistName"))
        title = normalize_audio_title(metadata.get("track"), artist)
        size_mb = metadata.get("size")
        size_hint = (
            int(size_mb * 1_000_000) if isinstance(size_mb, (int, float)) else None
        )
        duration = metadata.get("durationTime")
        asset = MediaAsset(
            source_url=media_url,
            kind=MediaKind.AUDIO,
            title=title,
            author=artist,
            duration_ms=duration * 1_000 if isinstance(duration, int) else None,
            size_hint=size_hint,
        )
        return MediaPost(
            source_url=url,
            platform=self.platform,
            assets=(asset,),
            title=asset.title,
            author=asset.author,
        )

    async def _media_url(
        self, track_id: str, track_file: dict, headers: dict[str, str]
    ) -> str:
        download_token = track_file.get("download")
        streaming_token = track_file.get("streaming")
        if isinstance(download_token, str) and download_token:
            response = await self.client.get(
                f"{_ZAYCEV_API_BASE}/track/download/{download_token}",
                headers=headers,
            )
            response.raise_for_status()
            media_url = response.text.strip()
        elif isinstance(streaming_token, str) and streaming_token:
            response = await self.client.get(
                f"{_ZAYCEV_API_BASE}/track/play/{streaming_token}",
                headers=headers,
            )
            response.raise_for_status()
            media_url = response.json().get("url")
        else:
            raise DownloadError(ErrorCode.UNAVAILABLE, "Zaycev.net track cannot be played")
        if not isinstance(media_url, str) or not media_url.startswith(("http://", "https://")):
            raise DownloadError(ErrorCode.UNAVAILABLE, "Zaycev.net returned an invalid media URL")
        return media_url


class DefaultPlatformRegistry:
    def __init__(
        self,
        *,
        cookies_file: str | None = None,
        cookies_files: Mapping[Platform, str | None] | None = None,
        client: httpx.AsyncClient | None = None,
        spotify_client_id: str | None = None,
        spotify_client_secret: str | None = None,
        spotify_market: str = "US",
        spotify_command: str | None = None,
        spotify_cache_dir: str = "spotify",
        spotify_resolve_timeout_seconds: int = 120,
        ytdlp_resolve_timeout_seconds: int = 120,
        youtube_pot_provider_url: str | None = None,
    ) -> None:
        provider_cookies = cookies_files or {}
        self._direct_media = DirectMediaPlatformAdapter(Platform.GENERIC)
        self._adapters: dict[Platform, PlatformAdapter] = {
            platform: YtDlpPlatformAdapter(
                platform,
                provider_cookies.get(platform) or cookies_file,
                youtube_pot_provider_url=youtube_pot_provider_url,
                resolve_timeout_seconds=ytdlp_resolve_timeout_seconds,
            )
            for platform in Platform
        }
        self._adapters[Platform.SPOTIFY] = SpotifyPlatformAdapter(
            platform=Platform.SPOTIFY,
            # Spotify fallback resolves catalog tracks through YouTube search.
            cookies_file=provider_cookies.get(Platform.YOUTUBE) or cookies_file,
            format_selector="bestaudio/best",
            client=client,
            client_id=spotify_client_id,
            client_secret=spotify_client_secret,
            market=spotify_market,
            command=spotify_command,
            cache_dir=spotify_cache_dir,
            resolve_timeout_seconds=spotify_resolve_timeout_seconds,
            youtube_pot_provider_url=youtube_pot_provider_url,
        )
        self._adapters[Platform.TWO_CH] = TwoChPlatformAdapter()
        if client:
            self._adapters[Platform.HITMOZ] = HitMozPlatformAdapter(
                platform=Platform.HITMOZ, client=client
            )
            self._adapters[Platform.ZAYCEV] = ZaycevPlatformAdapter(
                platform=Platform.ZAYCEV, client=client
            )

    def detect(self, url: str) -> PlatformAdapter:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if _is_hitmoz_host(host):
            return self._adapters[Platform.HITMOZ]
        for platform, domains in PLATFORM_DOMAINS.items():
            if any(host == domain or host.endswith(f".{domain}") for domain in domains):
                if platform is Platform.TWO_CH and not _is_direct_video_url(url):
                    return self._adapters[Platform.GENERIC]
                return self._adapters[platform]
        if _direct_media_kind(url) is not None:
            return self._direct_media
        if host:
            return self._adapters[Platform.GENERIC]
        raise DownloadError(ErrorCode.UNSUPPORTED, "Unsupported URL")


def _is_hitmoz_host(host: str) -> bool:
    labels = host.split(".")
    return len(labels) >= 2 and labels[-2] in {"hitmoz", "hitmos"}


def _is_direct_video_url(url: str) -> bool:
    return Path(urlsplit(url).path).suffix.lower() in DIRECT_VIDEO_EXTENSIONS


def _direct_media_kind(url: str) -> MediaKind | None:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in DIRECT_VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if suffix in DIRECT_AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    return None


def _two_ch_media_urls(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    domains = PLATFORM_DOMAINS[Platform.TWO_CH]
    ordered = (host, *(domain for domain in domains if domain != host))
    return tuple(
        urlunsplit(parsed._replace(netloc=f"{domain}{port}")) for domain in ordered
    )


def _native_spotify_asset(payload: dict, index: int) -> MediaAsset:
    identifier = payload.get("identifier")
    title = payload.get("title")
    author = normalize_artist_names(payload.get("author"))
    if not isinstance(identifier, str) or not isinstance(title, str):
        raise DownloadError(ErrorCode.UNAVAILABLE, "Spotify track metadata is invalid")
    title = normalize_audio_title(title, author)
    query = f"{author} - {title}" if author else title
    artwork_url = payload.get("artworkUrl") or payload.get("thumbnailUrl")
    return MediaAsset(
        source_url=identifier,
        kind=MediaKind.AUDIO,
        index=index,
        title=title,
        author=author,
        duration_ms=payload.get("durationMs") if isinstance(payload.get("durationMs"), int) else None,
        source=MediaSource.SPOTIFY_STREAM,
        fallback_query=query,
        thumbnail_url=artwork_url if isinstance(artwork_url, str) else None,
    )


def _spotify_image_url(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for image in payload.get("images") or ():
        if isinstance(image, dict) and isinstance(image.get("url"), str):
            return image["url"]
    return None


def _media_author(entry: dict) -> str | None:
    artist = normalize_artist_names(
        entry.get("artists") or entry.get("artist") or entry.get("album_artist")
    )
    if artist:
        return artist
    for key in ("artist", "creator", "uploader"):
        artist = normalize_artist_names(entry.get(key))
        if artist:
            return artist
    return None


def _duration_ms(value: object) -> int | None:
    if isinstance(value, (int, float)) and value >= 0:
        return round(value * 1_000)
    return None


def _media_chapters(data: Mapping[str, object]) -> tuple[MediaChapter, ...]:
    duration_ms = _duration_ms(data.get("duration"))
    description = data.get("description")
    if isinstance(description, str):
        parsed = _description_chapters(description, duration_ms)
        if len(parsed) >= 2:
            return parsed
    raw_chapters = data.get("chapters")
    if not isinstance(raw_chapters, list):
        return ()
    starts: list[tuple[int, str, int | None]] = []
    for raw in raw_chapters[:_MAX_DESCRIPTION_CHAPTERS]:
        if not isinstance(raw, Mapping):
            continue
        start_ms = _duration_ms(raw.get("start_time"))
        end_ms = _duration_ms(raw.get("end_time"))
        title = raw.get("title")
        if start_ms is None or not isinstance(title, str) or not title.strip():
            continue
        starts.append((start_ms, title.strip(), end_ms))
    return _chapter_boundaries(starts, duration_ms)


def _description_chapters(
    description: str, duration_ms: int | None
) -> tuple[MediaChapter, ...]:
    starts: list[tuple[int, str, int | None]] = []
    for line in description.splitlines():
        matches = tuple(_DESCRIPTION_TIMESTAMP_RE.finditer(line))
        if len(matches) != 1:
            continue
        match = matches[0]
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes"))
        seconds = int(match.group("seconds"))
        start_ms = (hours * 3600 + minutes * 60 + seconds) * 1_000
        title = f"{line[:match.start()]} {line[match.end():]}"
        title = re.sub(r"^[\s:|\-–—]+|[\s:|\-–—]+$", "", title)
        if not title or len(title) > 200:
            continue
        starts.append((start_ms, title, None))
        if len(starts) >= _MAX_DESCRIPTION_CHAPTERS:
            break
    if len(starts) < 2 or starts[0][0] > 5_000:
        return ()
    if starts[0][0] > 0:
        starts[0] = (0, starts[0][1], None)
    return _chapter_boundaries(starts, duration_ms)


def _chapter_boundaries(
    starts: list[tuple[int, str, int | None]], duration_ms: int | None
) -> tuple[MediaChapter, ...]:
    ordered: list[tuple[int, str, int | None]] = []
    for item in starts:
        if ordered and item[0] <= ordered[-1][0]:
            continue
        ordered.append(item)
    chapters: list[MediaChapter] = []
    for index, (start_ms, title, explicit_end_ms) in enumerate(ordered):
        next_start = ordered[index + 1][0] if index + 1 < len(ordered) else None
        end_ms = explicit_end_ms or next_start or duration_ms
        if end_ms is None or end_ms <= start_ms:
            continue
        chapters.append(MediaChapter(title, start_ms, end_ms))
    return tuple(chapters) if len(chapters) >= 2 else ()


def _request_headers(entry: dict, selected: dict) -> tuple[tuple[str, str], ...]:
    raw = selected.get("http_headers") or entry.get("http_headers") or {}
    headers = (
        {
            key: value
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, str) and value
        }
        if isinstance(raw, dict)
        else {}
    )
    raw_cookies = selected.get("cookies") or entry.get("cookies")
    if isinstance(raw_cookies, str) and raw_cookies:
        cookies = SimpleCookie()
        cookies.load(raw_cookies)
        cookie_header = "; ".join(
            f"{name}={morsel.value}" for name, morsel in cookies.items()
        )
        if cookie_header:
            headers["Cookie"] = cookie_header
    return tuple(headers.items())


def _spotify_oembed_author(payload: dict) -> str | None:
    author = payload.get("author_name")
    if (
        not isinstance(author, str)
        or not author.strip()
        or author.casefold() == "spotify"
    ):
        return None
    return author.strip()


def _hitmoz_candidates(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    suffix = parsed.path
    if parsed.query:
        suffix += f"?{parsed.query}"
    candidates = (url, *(f"{mirror}{suffix}" for mirror in _HITMOZ_MIRRORS))
    return tuple(dict.fromkeys(candidates))


def _hitmoz_http_error(exc: httpx.HTTPStatusError) -> DownloadError:
    status = exc.response.status_code
    if status == 429:
        return DownloadError(ErrorCode.RATE_LIMITED, "HitMoz rate limit exceeded", retryable=True)
    if status in {401, 403, 404}:
        return DownloadError(ErrorCode.UNAVAILABLE, "HitMoz album is unavailable")
    return DownloadError(ErrorCode.PROVIDER_FAILURE, f"HitMoz returned HTTP {status}", retryable=True)


def _format_selector(preferences: UserPreferences, *, audio_only: bool) -> str:
    if preferences.document_mode:
        return "bestaudio/best" if audio_only else "bestvideo*+bestaudio/best"
    if audio_only:
        return (
            "bestaudio[acodec^=mp4a]/bestaudio[acodec^=aac]/"
            "bestaudio[acodec=mp3]/bestaudio/best"
        )
    return (
        "bestvideo+bestaudio[acodec^=mp4a]/"
        "bestvideo+bestaudio[acodec^=aac]/"
        "bestvideo+bestaudio[acodec=mp3]/bestvideo+bestaudio/best"
    )


def _format_sort(
    *, audio_only: bool, quality: str = "best", document_mode: bool = False
) -> str:
    resolution = f"res:{quality}" if quality.isdigit() else "res"
    if document_mode:
        return (
            "lang,quality,abr,acodec"
            if audio_only
            else f"lang,quality,{resolution},fps,hdr:12,vcodec,acodec"
        )
    if audio_only:
        return "acodec:aac,lang,quality,abr"
    return f"vcodec:h264,lang,quality,{resolution},fps,hdr:12,acodec:aac"


def _hitmoz_download_links(page: str, base_url: str) -> tuple[str, ...]:
    seen: set[str] = set()
    links: list[str] = []
    for match in _HITMOZ_DOWNLOAD_PATTERN.finditer(page):
        link = urljoin(base_url, html.unescape(match.group("url")))
        if link not in seen:
            seen.add(link)
            links.append(link)
    return tuple(links)


def _hitmoz_track_title(download_url: str) -> str:
    filename = urlsplit(download_url).path.rsplit("/", 1)[-1].removesuffix(".mp3")
    filename = _HITMOZ_FILENAME_PATTERN.sub("", filename)
    return filename.replace("_-_", " - ").replace("_", " ").strip()


def _hitmoz_asset(download_url: str, index: int) -> MediaAsset:
    raw_title = _hitmoz_track_title(download_url)
    split = split_audio_artist_title(raw_title)
    author, title = split if split else (None, raw_title)
    return MediaAsset(
        source_url=download_url,
        kind=MediaKind.AUDIO,
        index=index,
        title=normalize_audio_title(title, author),
        author=normalize_artist_names(author),
    )


def _html_title(page: str) -> str | None:
    match = _HTML_TITLE_PATTERN.search(page)
    if not match:
        return None
    title = re.sub(r"\s+", " ", html.unescape(match.group("title"))).strip()
    return title or None
