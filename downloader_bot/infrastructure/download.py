from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import sys
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath

import httpx

from downloader_bot.domain import (
    DownloadArtifact,
    ErrorCode,
    Job,
    JobStage,
    MediaKind,
    MediaPost,
    MediaSource,
    Platform,
    Progress,
)
from downloader_bot.domain.audio_names import build_audio_filename
from downloader_bot.domain.errors import DownloadError
from downloader_bot.infrastructure.cookies import writable_cookie_file


class HttpDownloadEngine:
    def __init__(
        self,
        client: httpx.AsyncClient,
        root: Path,
        *,
        max_file_size: int = 2_000_000_000,
        max_parallel_downloads: int = 4,
        spotify_command: str = "spotify-streamer",
        spotify_cache_dir: str = "spotify",
        spotify_bitrate: int = 320,
    ) -> None:
        if max_parallel_downloads < 1:
            raise ValueError("max_parallel_downloads must be at least 1")
        self._client = client
        self._root = root
        self._max_file_size = max_file_size
        self._semaphore = asyncio.Semaphore(max_parallel_downloads)
        self._spotify_command = spotify_command
        self._spotify_cache_dir = spotify_cache_dir
        self._spotify_bitrate = spotify_bitrate

    async def download(
        self, post: MediaPost, job: Job, progress, cancellation
    ) -> tuple[DownloadArtifact, ...]:
        directory = self._root / job.id
        directory.mkdir(parents=True, exist_ok=True)
        tasks = [
            asyncio.create_task(
                self._download_asset(post, job, asset, item, directory, progress, cancellation)
            )
            for item, asset in enumerate(post.assets, 1)
        ]
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _download_asset(
        self, post: MediaPost, job: Job, asset, item: int, directory: Path, progress, cancellation
    ) -> DownloadArtifact:
        async with self._semaphore:
            if await cancellation.requested():
                raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
            if asset.source is MediaSource.SPOTIFY_STREAM:
                try:
                    return await self._download_spotify_stream(
                        post, job, asset, item, directory, progress, cancellation
                    )
                except DownloadError as exc:
                    if exc.code in {ErrorCode.CANCELLED, ErrorCode.TOO_LARGE} or not asset.fallback_query:
                        raise
                    return await self._download_youtube_fallback(
                        post, job, asset, item, directory, progress, cancellation
                    )
            suffix = _suffix(asset.source_url, asset.kind.value)
            target = _asset_path(
                directory, asset, suffix, audio_only=job.audio_only
            )
            partial = target.with_suffix(f"{target.suffix}.part")
            digest = hashlib.sha256()
            written = 0
            headers = _download_headers(post, asset.source_url)
            headers.update(asset.request_headers)
            if partial.exists():
                written = partial.stat().st_size
                headers["Range"] = f"bytes={written}-"
                with partial.open("rb") as existing:
                    while chunk := existing.read(1024 * 1024):
                        digest.update(chunk)
            async with self._client.stream(
                "GET", asset.source_url, headers=headers, follow_redirects=True
            ) as response:
                if response.status_code == 416:
                    partial.replace(target)
                else:
                    if response.status_code == 403 and asset.extractor_url:
                        target = await self._download_with_ytdlp(
                            asset,
                            job,
                            item,
                            len(post.assets),
                            directory,
                            progress,
                            cancellation,
                        )
                        return await self._finalize(target, asset, job, cancellation)
                    response.raise_for_status()
                    if _is_hls_playlist(response):
                        target = target.with_suffix(".mp4")
                        await _download_hls(
                            asset.source_url,
                            target,
                            job,
                            item,
                            len(post.assets),
                            progress,
                            cancellation,
                            self._max_file_size,
                            headers,
                        )
                        return await self._finalize(target, asset, job, cancellation)
                    total = _total_size(response, written)
                    if total and total > self._max_file_size:
                        raise DownloadError(
                            ErrorCode.TOO_LARGE,
                            "Media exceeds the configured size limit",
                        )
                    mode = "ab" if response.status_code == 206 and written else "wb"
                    if mode == "wb":
                        written = 0
                        digest = hashlib.sha256()
                    started_at = time.monotonic()
                    started_bytes = written
                    with partial.open(mode) as output:
                        async for chunk in response.aiter_bytes(256 * 1024):
                            if await cancellation.requested():
                                raise DownloadError(
                                    ErrorCode.CANCELLED, "Download cancelled"
                                )
                            written += len(chunk)
                            if written > self._max_file_size:
                                raise DownloadError(
                                    ErrorCode.TOO_LARGE,
                                    "Media exceeds the configured size limit",
                                )
                            output.write(chunk)
                            digest.update(chunk)
                            percent = (
                                min(99, int(written * 100 / total)) if total else 0
                            )
                            elapsed = max(0.001, time.monotonic() - started_at)
                            speed = max(0, int((written - started_bytes) / elapsed))
                            eta = (
                                max(0, int((total - written) / speed))
                                if total and speed
                                else None
                            )
                            await progress(
                                Progress(
                                    job_id=job.id,
                                    stage=JobStage.DOWNLOADING,
                                    percent=percent,
                                    attempt=job.attempt,
                                    item=item,
                                    item_count=len(post.assets),
                                    downloaded_bytes=written,
                                    total_bytes=total,
                                    speed_bytes_per_second=speed or None,
                                    eta_seconds=eta,
                                )
                            )
                    partial.replace(target)
            return await self._finalize(target, asset, job, cancellation)

    async def _download_with_ytdlp(
        self,
        asset,
        job: Job,
        item: int,
        item_count: int,
        directory: Path,
        progress,
        cancellation,
    ) -> Path:
        readable = _asset_path(
            directory, asset, ".source", audio_only=job.audio_only
        )
        output_template = readable.with_name(f"{readable.stem}.ytdlp.%(ext)s")
        with writable_cookie_file(asset.cookies_file) as cookie_file:
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--quiet",
                "--no-warnings",
                "--no-playlist",
                "--format",
                asset.format_selector
                or ("bestaudio/best" if job.audio_only else "best"),
                "--max-filesize",
                str(self._max_file_size),
                "--extractor-args",
                f"youtube:player_client={os.getenv('YTDLP_YOUTUBE_PLAYER_CLIENT', 'mweb')}",
                "--output",
                str(output_template),
                asset.extractor_url,
            ]
            if cookie_file:
                command[-1:-1] = ["--cookies", cookie_file]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            while process.returncode is None:
                if await cancellation.requested():
                    process.terminate()
                    await process.wait()
                    _cleanup_ytdlp_files(output_template)
                    raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
                await progress(
                    Progress(
                        job_id=job.id,
                        stage=JobStage.DOWNLOADING,
                        percent=0,
                        attempt=job.attempt,
                        item=item,
                        item_count=item_count,
                    )
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.25)
                except TimeoutError:
                    continue
            if process.returncode:
                detail = (
                    (await process.stderr.read()).decode(errors="replace")[-1000:]
                    if process.stderr
                    else ""
                )
                _cleanup_ytdlp_files(output_template)
                raise DownloadError(
                    ErrorCode.UNAVAILABLE,
                    detail or "yt-dlp download failed",
                    retryable=True,
                )
        candidates = tuple(
            path
            for path in directory.glob(f"{readable.stem}.ytdlp.*")
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        )
        if not candidates:
            raise DownloadError(ErrorCode.UNAVAILABLE, "yt-dlp produced no media")
        target = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        if target.stat().st_size > self._max_file_size:
            _cleanup_ytdlp_files(output_template)
            raise DownloadError(ErrorCode.TOO_LARGE, "Media exceeds the configured size limit")
        final_source = readable.with_suffix(target.suffix)
        target.replace(final_source)
        return final_source

    async def _download_youtube_fallback(
        self, post: MediaPost, job: Job, asset, item: int, directory: Path, progress, cancellation
    ) -> DownloadArtifact:
        with writable_cookie_file(asset.cookies_file) as cookie_file:
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--dump-single-json",
                "--no-warnings",
                "--format",
                "bestaudio/best",
            ]
            if cookie_file:
                command.extend(("--cookies", cookie_file))
            command.extend(
                (
                    "--extractor-args",
                    f"youtube:player_client={os.getenv('YTDLP_YOUTUBE_PLAYER_CLIENT', 'mweb')}",
                    f"ytsearch1:{asset.fallback_query}",
                )
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        if process.returncode:
            raise DownloadError(
                ErrorCode.UNAVAILABLE,
                stderr.decode(errors="replace").strip()[-1000:] or "YouTube fallback failed",
            )
        data = json.loads(stdout)
        entry = (data.get("entries") or (data,))[0]
        source_url = entry.get("url")
        if not isinstance(source_url, str):
            raise DownloadError(ErrorCode.UNAVAILABLE, "YouTube fallback returned no audio URL")
        http_asset = replace(asset, source_url=source_url, source=MediaSource.HTTP)
        return await self._download_asset(post, job, http_asset, item, directory, progress, cancellation)

    async def _download_spotify_stream(
        self, post: MediaPost, job: Job, asset, item: int, directory: Path, progress, cancellation
    ) -> DownloadArtifact:
        target = _asset_path(directory, asset, ".wav", audio_only=job.audio_only)
        spotify = await asyncio.create_subprocess_exec(
            self._spotify_command,
            "stream",
            "--cache",
            self._spotify_cache_dir,
            "--uri",
            asset.source_url,
            "--bitrate",
            str(self._spotify_bitrate),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-i",
            "pipe:0",
            "-vn",
            "-metadata",
            f"title={asset.title or ''}",
            "-metadata",
            f"artist={asset.author or ''}",
            str(target),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert spotify.stdout is not None and ffmpeg.stdin is not None
        bridge = asyncio.create_task(_pipe(spotify.stdout, ffmpeg.stdin))
        spotify_stderr = asyncio.create_task(spotify.stderr.read() if spotify.stderr else _empty_bytes())
        ffmpeg_stderr = asyncio.create_task(ffmpeg.stderr.read() if ffmpeg.stderr else _empty_bytes())
        try:
            while spotify.returncode is None or ffmpeg.returncode is None:
                if await cancellation.requested():
                    await _terminate(spotify, ffmpeg)
                    target.unlink(missing_ok=True)
                    raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
                if target.exists() and target.stat().st_size > self._max_file_size:
                    await _terminate(spotify, ffmpeg)
                    target.unlink(missing_ok=True)
                    raise DownloadError(ErrorCode.TOO_LARGE, "Media exceeds the configured size limit")
                await progress(
                    Progress(
                        job.id,
                        JobStage.DOWNLOADING,
                        percent=_spotify_stream_percent(target, asset.duration_ms),
                        attempt=job.attempt,
                        item=item,
                        item_count=len(post.assets),
                    )
                )
                await asyncio.sleep(0.25)
            await bridge
            await spotify.wait()
            await ffmpeg.wait()
            errors = (await spotify_stderr) + (await ffmpeg_stderr)
            if spotify.returncode or ffmpeg.returncode:
                target.unlink(missing_ok=True)
                raise DownloadError(
                    ErrorCode.PROVIDER_FAILURE,
                    errors.decode(errors="replace").strip()[-1000:] or "Native Spotify stream failed",
                )
            if not target.exists():
                raise DownloadError(ErrorCode.PROVIDER_FAILURE, "Native Spotify stream produced no audio")
            return await self._finalize(target, asset, job, cancellation)
        finally:
            if spotify.returncode is None or ffmpeg.returncode is None:
                await _terminate(spotify, ffmpeg)
            for task in (bridge, spotify_stderr, ffmpeg_stderr):
                if not task.done():
                    task.cancel()


    async def _finalize(self, target: Path, asset, job: Job, cancellation) -> DownloadArtifact:
        kind = MediaKind.AUDIO if job.audio_only else asset.kind
        if kind is MediaKind.AUDIO:
            target = await _transcode_audio(
                target,
                cancellation,
                title=asset.title,
                author=asset.author,
            )
        elif kind is MediaKind.VIDEO:
            target = await _transcode_video(target, cancellation)
        digest = hashlib.sha256()
        with target.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        thumbnail = await self._thumbnail(asset.thumbnail_url, target, cancellation)
        return DownloadArtifact(
            path=PurePosixPath(target),
            kind=kind,
            size=target.stat().st_size,
            checksum=digest.hexdigest(),
            mime_type=mimetypes.guess_type(target.name)[0],
            title=asset.title,
            author=asset.author,
            duration_ms=asset.duration_ms,
            thumbnail_path=PurePosixPath(thumbnail) if thumbnail else None,
        )

    async def _thumbnail(self, url: str | None, media: Path, cancellation) -> Path | None:
        if not url or await cancellation.requested():
            return None
        source = media.with_suffix(".thumbnail.source")
        target = media.with_suffix(".thumbnail.jpg")
        try:
            response = await self._client.get(url, follow_redirects=True, timeout=20)
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                return None
            source.write_bytes(response.content)
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vf",
                "scale=320:320:force_original_aspect_ratio=decrease",
                "-frames:v",
                "1",
                "-q:v",
                "6",
                str(target),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            if process.returncode == 0 and target.is_file() and target.stat().st_size < 200_000:
                return target
        except (httpx.HTTPError, OSError):
            return None
        finally:
            source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        return None


def _suffix(url: str, kind: str) -> str:
    suffix = Path(httpx.URL(url).path).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    return ".mp3" if kind == "audio" else ".mp4"


def _asset_path(
    directory: Path, asset, suffix: str, *, audio_only: bool = False
) -> Path:
    if (audio_only or asset.kind is MediaKind.AUDIO) and asset.title:
        return directory / build_audio_filename(
            asset.title, asset.author, suffix=suffix
        )
    return directory / f"{asset.index:03d}{suffix}"


def _spotify_stream_percent(target: Path, duration_ms: int | None) -> int:
    if not duration_ms or not target.exists():
        return 0
    expected_bytes = duration_ms * 44_100 * 2 * 2 // 1_000
    if expected_bytes <= 0:
        return 0
    return min(99, int(target.stat().st_size * 100 / expected_bytes))


def _cleanup_ytdlp_files(output_template: Path) -> None:
    pattern = output_template.name.replace("%(ext)s", "*")
    for path in output_template.parent.glob(pattern):
        path.unlink(missing_ok=True)


def _download_headers(post: MediaPost, media_url: str | None = None) -> dict[str, str]:
    if post.platform is not Platform.HITMOZ:
        return {}
    source = httpx.URL(media_url or post.source_url)
    return {
        "Accept": "audio/mpeg,audio/*;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        "Referer": f"{source.scheme}://{source.host}/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _total_size(response: httpx.Response, resumed: int) -> int | None:
    value = response.headers.get("content-length")
    return resumed + int(value) if value and value.isdigit() else None


def _is_hls_playlist(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return any(
        media_type in content_type
        for media_type in (
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
            "audio/mpegurl",
        )
    )


async def _download_hls(
    source_url: str,
    target: Path,
    job: Job,
    item: int,
    item_count: int,
    progress,
    cancellation,
    max_file_size: int,
    headers: dict[str, str] | None = None,
) -> None:
    partial = target.with_suffix(f".part{target.suffix}")
    partial.unlink(missing_ok=True)
    command = ["ffmpeg", "-y"]
    if headers:
        command.extend(
            ("-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items()))
        )
    command.extend(
        (
            "-i",
            source_url,
            "-c",
            "copy",
            str(partial),
        )
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    while process.returncode is None:
        if await cancellation.requested():
            process.terminate()
            await process.wait()
            partial.unlink(missing_ok=True)
            raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
        if partial.exists() and partial.stat().st_size > max_file_size:
            process.terminate()
            await process.wait()
            partial.unlink(missing_ok=True)
            raise DownloadError(
                ErrorCode.TOO_LARGE,
                "Media exceeds the configured size limit",
            )
        await progress(
            Progress(
                job_id=job.id,
                stage=JobStage.DOWNLOADING,
                percent=0,
                attempt=job.attempt,
                item=item,
                item_count=item_count,
            )
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=0.25)
        except TimeoutError:
            continue
    if process.returncode:
        stderr = process.stderr
        detail = (
            (await stderr.read()).decode(errors="replace")[-1000:]
            if stderr is not None
            else ""
        )
        partial.unlink(missing_ok=True)
        raise DownloadError(
            ErrorCode.PROVIDER_FAILURE,
            detail or "HLS download failed",
            retryable=False,
        )
    if partial.stat().st_size > max_file_size:
        partial.unlink(missing_ok=True)
        raise DownloadError(ErrorCode.TOO_LARGE, "Media exceeds the configured size limit")
    partial.replace(target)


async def _transcode_audio(
    target: Path,
    cancellation,
    *,
    title: str | None = None,
    author: str | None = None,
) -> Path:
    output = target.with_name(f"{target.stem}.converted.m4a")
    final = target.with_suffix(".m4a")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(target),
        "-vn",
        "-codec:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-map_metadata",
        "0",
        "-metadata",
        f"title={title or ''}",
        "-metadata",
        f"artist={author or ''}",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    while process.returncode is None:
        if await cancellation.requested():
            process.terminate()
            await process.wait()
            output.unlink(missing_ok=True)
            raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
        try:
            await asyncio.wait_for(process.wait(), timeout=0.25)
        except TimeoutError:
            continue
    if process.returncode:
        stderr = process.stderr
        detail = (
            (await stderr.read()).decode(errors="replace")[-1000:]
            if stderr is not None
            else ""
        )
        output.unlink(missing_ok=True)
        raise DownloadError(
            ErrorCode.PROVIDER_FAILURE,
            detail or "Audio conversion failed",
            retryable=False,
        )
    target.unlink(missing_ok=True)
    output.replace(final)
    return final


async def _empty_bytes() -> bytes:
    return b""


async def _pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(256 * 1024):
            target.write(chunk)
            await target.drain()
    finally:
        target.close()


async def _terminate(*processes: asyncio.subprocess.Process) -> None:
    for process in processes:
        if process.returncode is None:
            process.terminate()
    await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)


async def _transcode_video(target: Path, cancellation) -> Path:
    output = target.with_name(f"{target.stem}.converted.mp4")
    final = target.with_suffix(".mp4")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(target),
        "-codec:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-codec:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    while process.returncode is None:
        if await cancellation.requested():
            process.terminate()
            await process.wait()
            output.unlink(missing_ok=True)
            raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
        try:
            await asyncio.wait_for(process.wait(), timeout=0.25)
        except TimeoutError:
            continue
    if process.returncode:
        stderr = process.stderr
        detail = (
            (await stderr.read()).decode(errors="replace")[-1000:]
            if stderr is not None
            else ""
        )
        output.unlink(missing_ok=True)
        raise DownloadError(
            ErrorCode.PROVIDER_FAILURE,
            detail or "Video conversion failed",
            retryable=False,
        )
    target.unlink(missing_ok=True)
    output.replace(final)
    return final
