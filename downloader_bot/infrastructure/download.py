from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
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
    MediaChapter,
    MediaKind,
    MediaPost,
    MediaSource,
    Platform,
    Progress,
)
from downloader_bot.domain.audio_names import build_audio_filename
from downloader_bot.domain.errors import DownloadError
from downloader_bot.infrastructure.cookies import writable_cookie_file

_YTDLP_PROGRESS_PREFIX = "__MEDIA_POCKET_PROGRESS__"
_CANCELLATION_POLL_SECONDS = 0.5


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
        youtube_pot_provider_url: str | None = None,
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
        self._youtube_pot_provider_url = youtube_pot_provider_url

    def _youtube_extractor_args(self) -> list[str]:
        args = [
            "--extractor-args",
            "youtube:player_client=mweb",
        ]
        if self._youtube_pot_provider_url:
            args.extend(
                (
                    "--extractor-args",
                    f"youtubepot-bgutilhttp:base_url={self._youtube_pot_provider_url}",
                )
            )
        return args

    async def download(
        self, post: MediaPost, job: Job, progress, cancellation
    ) -> tuple[DownloadArtifact, ...]:
        directory = self._root / job.id
        directory.mkdir(parents=True, exist_ok=True)
        tasks = [
            asyncio.create_task(
                self._download_asset_with_fallback(
                    post, job, asset, item, directory, progress, cancellation
                )
            )
            for item, asset in enumerate(post.assets, 1)
        ]
        try:
            artifacts = tuple(await asyncio.gather(*tasks))
            if job.preferences.split_chapters:
                return await self._split_chapters(
                    post, artifacts, job, cancellation
                )
            return artifacts
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _split_chapters(
        self,
        post: MediaPost,
        artifacts: tuple[DownloadArtifact, ...],
        job: Job,
        cancellation,
    ) -> tuple[DownloadArtifact, ...]:
        if not job.audio_only or len(artifacts) != 1 or len(post.chapters) < 2:
            raise DownloadError(
                ErrorCode.UNAVAILABLE,
                "YouTube timestamps were no longer available for splitting",
                retryable=False,
            )
        source_artifact = artifacts[0]
        source = Path(source_artifact.path)
        created: list[DownloadArtifact] = []
        try:
            for index, chapter in enumerate(post.chapters, 1):
                created.append(
                    await _split_audio_chapter(
                        source,
                        chapter,
                        index,
                        source_artifact.author,
                        source_artifact.thumbnail_path,
                        self._max_file_size,
                        cancellation,
                    )
                )
        except BaseException:
            for artifact in created:
                Path(artifact.path).unlink(missing_ok=True)
            raise
        source.unlink(missing_ok=True)
        return tuple(created)

    async def _download_asset_with_fallback(
        self, post, job: Job, asset, item: int, directory: Path, progress, cancellation
    ) -> DownloadArtifact:
        candidates = (asset.source_url, *asset.fallback_urls)
        for index, source_url in enumerate(candidates):
            candidate = replace(asset, source_url=source_url, fallback_urls=())
            try:
                return await self._download_asset(
                    post, job, candidate, item, directory, progress, cancellation
                )
            except httpx.HTTPError as exc:
                if index == len(candidates) - 1 or not _can_retry_mirror(exc):
                    raise
        raise AssertionError("direct media candidates cannot be empty")

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
            if asset.extractor_url and (
                post.platform is Platform.YOUTUBE
                or asset.requires_extractor_download
            ):
                target = await self._download_with_ytdlp(
                    asset,
                    job,
                    item,
                    len(post.assets),
                    directory,
                    progress,
                    cancellation,
                )
                return await self._finalize(
                    target, asset, job, progress, item, len(post.assets), cancellation
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
                        return await self._finalize(
                            target,
                            asset,
                            job,
                            progress,
                            item,
                            len(post.assets),
                            cancellation,
                        )
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
                        return await self._finalize(
                            target,
                            asset,
                            job,
                            progress,
                            item,
                            len(post.assets),
                            cancellation,
                        )
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
                    chunks = response.aiter_bytes(256 * 1024)
                    with partial.open(mode) as output:
                        while (
                            chunk := await _next_chunk(chunks, cancellation)
                        ) is not None:
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
                                    elapsed_seconds=max(0, int(elapsed)),
                                    indeterminate=total is None,
                                )
                            )
                    partial.replace(target)
            return await self._finalize(
                target, asset, job, progress, item, len(post.assets), cancellation
            )

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
        if asset.requires_extractor_download:
            return await self._download_merged_with_pipe(
                asset,
                job,
                item,
                item_count,
                directory,
                progress,
                cancellation,
            )
        readable = _asset_path(
            directory, asset, ".source", audio_only=job.audio_only
        )
        output_template = readable.with_name(f"{readable.stem}.ytdlp.%(ext)s")
        with writable_cookie_file(asset.cookies_file) as cookie_file:
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--ignore-config",
                "--quiet",
                "--no-warnings",
                "--progress",
                "--newline",
                "--progress-delta",
                "0.5",
                "--progress-template",
                f"download:{_YTDLP_PROGRESS_PREFIX}%(progress)j",
                "--no-playlist",
                "--format",
                asset.format_selector
                or (
                    "bestaudio[acodec^=mp4a]/bestaudio[acodec^=aac]/"
                    "bestaudio[acodec=mp3]/bestaudio/best"
                    if job.audio_only
                    else "bestvideo+bestaudio[acodec^=mp4a]/"
                    "bestvideo+bestaudio[acodec^=aac]/"
                    "bestvideo+bestaudio[acodec=mp3]/bestvideo+bestaudio/best"
                ),
                "--max-filesize",
                str(self._max_file_size),
                "--output",
                str(output_template),
                asset.extractor_url,
            ]
            command[-3:-3] = [
                "--format-sort",
                asset.format_sort
                or (
                    "acodec:aac,lang,quality,abr"
                    if job.audio_only
                    else "vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac"
                ),
            ]
            if not job.audio_only:
                command[-3:-3] = [
                    "--merge-output-format",
                    "mp4/mkv",
                    "--postprocessor-args",
                    "Merger+ffmpeg_o:-movflags +faststart",
                ]
            command[-3:-3] = self._youtube_extractor_args()
            if cookie_file:
                command[-1:-1] = ["--cookies", cookie_file]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None
            progress_task = asyncio.create_task(
                _read_ytdlp_progress(
                    process.stdout, job, item, item_count, progress
                )
            )
            stderr_task = asyncio.create_task(process.stderr.read())
            try:
                while process.returncode is None:
                    if await cancellation.requested():
                        await _terminate(process)
                        _cleanup_ytdlp_files(output_template)
                        raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.25)
                    except TimeoutError:
                        continue
                await progress_task
                detail = (await stderr_task).decode(errors="replace")[-1000:]
            finally:
                for task in (progress_task, stderr_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    progress_task, stderr_task, return_exceptions=True
                )
            if process.returncode:
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

    async def _download_merged_with_pipe(
        self,
        asset,
        job: Job,
        item: int,
        item_count: int,
        directory: Path,
        progress,
        cancellation,
    ) -> Path:
        kind = MediaKind.AUDIO if job.audio_only else asset.kind
        suffix = ".m4a" if kind is MediaKind.AUDIO else ".mp4"
        target = _asset_path(directory, asset, suffix, audio_only=job.audio_only)
        format_selector = asset.format_selector or "bestvideo+bestaudio/best"
        if kind is MediaKind.VIDEO:
            format_selector = (
                "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/"
                "bestvideo[vcodec^=avc]+bestaudio[acodec^=aac]/"
                f"best[vcodec^=avc]/best/{format_selector}"
            )
        with writable_cookie_file(asset.cookies_file) as cookie_file:
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--ignore-config",
                "--quiet",
                "--no-warnings",
                "--no-progress",
                "--no-playlist",
                "--format",
                format_selector,
                "--format-sort",
                asset.format_sort
                or (
                    "acodec:aac,lang,quality,abr"
                    if job.audio_only
                    else "vcodec:h264,lang,quality,res,fps,hdr:12,acodec:aac"
                ),
                "--max-filesize",
                str(self._max_file_size),
                "--output",
                "-",
            ]
            command.extend(self._youtube_extractor_args())
            if cookie_file:
                command.extend(("--cookies", cookie_file))
            command.append(asset.extractor_url)
            ytdlp = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            codec_args = (
                (
                    "-vn",
                    "-map",
                    "0:a:0",
                    "-codec:a",
                    "aac",
                    "-profile:a",
                    "aac_low",
                    "-aac_coder",
                    "fast",
                )
                if kind is MediaKind.AUDIO
                else (
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-codec:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "2",
                    "-crf",
                    "23",
                    "-codec:a",
                    "aac",
                    "-profile:a",
                    "aac_low",
                    "-aac_coder",
                    "fast",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                )
            )
            ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                *codec_args,
                str(target),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            assert ytdlp.stdout is not None and ffmpeg.stdin is not None
            bridge = asyncio.create_task(_pipe(ytdlp.stdout, ffmpeg.stdin))
            ytdlp_stderr = asyncio.create_task(
                ytdlp.stderr.read() if ytdlp.stderr else _empty_bytes()
            )
            ffmpeg_stderr = asyncio.create_task(
                ffmpeg.stderr.read() if ffmpeg.stderr else _empty_bytes()
            )
            started_at = time.monotonic()
            try:
                while ytdlp.returncode is None or ffmpeg.returncode is None:
                    if await cancellation.requested():
                        await _terminate(ytdlp, ffmpeg)
                        target.unlink(missing_ok=True)
                        raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
                    downloaded = target.stat().st_size if target.exists() else 0
                    if downloaded > self._max_file_size:
                        await _terminate(ytdlp, ffmpeg)
                        target.unlink(missing_ok=True)
                        raise DownloadError(
                            ErrorCode.TOO_LARGE,
                            "Media exceeds the configured size limit",
                        )
                    elapsed = max(0.001, time.monotonic() - started_at)
                    await progress(
                        Progress(
                            job_id=job.id,
                            stage=JobStage.DOWNLOADING,
                            attempt=job.attempt,
                            item=item,
                            item_count=item_count,
                            downloaded_bytes=downloaded,
                            speed_bytes_per_second=int(downloaded / elapsed) or None,
                            elapsed_seconds=int(elapsed),
                            indeterminate=True,
                        )
                    )
                    await asyncio.sleep(0.25)
                await asyncio.gather(bridge, return_exceptions=True)
                errors = (await ytdlp_stderr) + (await ffmpeg_stderr)
                if ytdlp.returncode or ffmpeg.returncode:
                    target.unlink(missing_ok=True)
                    raise DownloadError(
                        ErrorCode.UNAVAILABLE,
                        errors.decode(errors="replace").strip()[-1000:]
                        or "Provider stream conversion failed",
                        retryable=True,
                    )
                if not target.is_file():
                    raise DownloadError(
                        ErrorCode.UNAVAILABLE,
                        "Provider stream conversion produced no media",
                        retryable=True,
                    )
                return target
            finally:
                if ytdlp.returncode is None or ffmpeg.returncode is None:
                    await _terminate(ytdlp, ffmpeg)
                for task in (bridge, ytdlp_stderr, ffmpeg_stderr):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    bridge, ytdlp_stderr, ffmpeg_stderr, return_exceptions=True
                )

    async def _download_youtube_fallback(
        self, post: MediaPost, job: Job, asset, item: int, directory: Path, progress, cancellation
    ) -> DownloadArtifact:
        with writable_cookie_file(asset.cookies_file) as cookie_file:
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--ignore-config",
                "--dump-single-json",
                "--no-warnings",
                "--format",
                (
                    "bestaudio[acodec^=mp4a]/bestaudio[acodec^=aac]/"
                    "bestaudio[acodec=mp3]/bestaudio/best"
                ),
                "--format-sort",
                "acodec:aac,lang,quality,abr",
            ]
            if cookie_file:
                command.extend(("--cookies", cookie_file))
            command.extend(self._youtube_extractor_args())
            command.append(f"ytsearch1:{asset.fallback_query}")
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
        started_at = time.monotonic()
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
                elapsed = max(0.001, time.monotonic() - started_at)
                downloaded = target.stat().st_size if target.exists() else 0
                expected = _spotify_expected_bytes(asset.duration_ms)
                speed = int(downloaded / elapsed)
                await progress(
                    Progress(
                        job.id,
                        JobStage.DOWNLOADING,
                        percent=_spotify_stream_percent(target, asset.duration_ms),
                        attempt=job.attempt,
                        item=item,
                        item_count=len(post.assets),
                        downloaded_bytes=downloaded,
                        total_bytes=expected,
                        speed_bytes_per_second=speed or None,
                        eta_seconds=(
                            max(0, int((expected - downloaded) / speed))
                            if expected and speed
                            else None
                        ),
                        elapsed_seconds=int(elapsed),
                        indeterminate=expected is None,
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
            return await self._finalize(
                target, asset, job, progress, item, len(post.assets), cancellation
            )
        finally:
            if spotify.returncode is None or ffmpeg.returncode is None:
                await _terminate(spotify, ffmpeg)
            for task in (bridge, spotify_stderr, ffmpeg_stderr):
                if not task.done():
                    task.cancel()


    async def _finalize(
        self,
        target: Path,
        asset,
        job: Job,
        progress,
        item: int,
        item_count: int,
        cancellation,
    ) -> DownloadArtifact:
        kind = MediaKind.AUDIO if job.audio_only else asset.kind
        audio_strategy = (
            await _audio_strategy(target)
            if kind is MediaKind.AUDIO and not job.preferences.document_mode
            else None
        )
        video_strategy = (
            await _video_strategy(target)
            if kind is MediaKind.VIDEO and not job.preferences.document_mode
            else None
        )
        processing_detail = (
            "preparing_document"
            if job.preferences.document_mode
            else "converting_audio"
            if audio_strategy == "transcode"
            else "converting_video"
            if video_strategy in {"transcode_audio", "transcode_video", "transcode"}
            else f"preparing_{kind.value}"
        )

        async def report_processing(elapsed_seconds: int = 0) -> None:
            await progress(
                Progress(
                    job_id=job.id,
                    stage=JobStage.PROCESSING,
                    attempt=job.attempt,
                    item=item,
                    item_count=item_count,
                    detail=processing_detail,
                    elapsed_seconds=elapsed_seconds,
                    indeterminate=True,
                )
            )

        await report_processing()
        thumbnail = await self._thumbnail(asset.thumbnail_url, target, cancellation)
        if kind is MediaKind.AUDIO and not job.preferences.document_mode:
            target = await _transcode_audio(
                target,
                cancellation,
                title=asset.title,
                author=asset.author,
                cover=thumbnail,
                report=report_processing,
                strategy=audio_strategy,
            )
        elif kind is MediaKind.VIDEO and not job.preferences.document_mode:
            target = await _transcode_video(
                target,
                cancellation,
                report=report_processing,
                strategy=video_strategy,
            )
        if target.stat().st_size > self._max_file_size:
            target.unlink(missing_ok=True)
            raise DownloadError(
                ErrorCode.TOO_LARGE,
                "Prepared media exceeds the configured size limit",
            )
        digest = hashlib.sha256()
        with target.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
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
    expected_bytes = _spotify_expected_bytes(duration_ms)
    if not expected_bytes or not target.exists():
        return 0
    return min(99, int(target.stat().st_size * 100 / expected_bytes))


async def _next_chunk(chunks, cancellation) -> bytes | None:
    task = asyncio.create_task(anext(chunks))
    try:
        while True:
            done, _ = await asyncio.wait(
                {task}, timeout=_CANCELLATION_POLL_SECONDS
            )
            if task in done:
                try:
                    return task.result()
                except StopAsyncIteration:
                    return None
            if await cancellation.requested():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _spotify_expected_bytes(duration_ms: int | None) -> int | None:
    if not duration_ms:
        return None
    expected = duration_ms * 44_100 * 2 * 2 // 1_000
    return expected if expected > 0 else None


async def _read_ytdlp_progress(
    stream: asyncio.StreamReader,
    job: Job,
    item: int,
    item_count: int,
    report,
) -> None:
    while line := await stream.readline():
        value = line.decode(errors="replace").strip()
        if not value.startswith(_YTDLP_PROGRESS_PREFIX):
            continue
        try:
            data = json.loads(value.removeprefix(_YTDLP_PROGRESS_PREFIX))
        except (json.JSONDecodeError, TypeError):
            continue
        event = _ytdlp_progress_event(data, job, item, item_count)
        if event is not None:
            await report(event)


def _ytdlp_progress_event(
    data: object, job: Job, item: int, item_count: int
) -> Progress | None:
    if not isinstance(data, dict) or data.get("status") not in {
        "downloading",
        "finished",
    }:
        return None
    downloaded = _positive_int(data.get("downloaded_bytes"), allow_zero=True)
    exact_total = _positive_int(data.get("total_bytes"))
    estimated_total = _positive_int(data.get("total_bytes_estimate"))
    total = exact_total or estimated_total
    percent_value = _number(data.get("_percent"))
    fragment_index = _positive_int(data.get("fragment_index"), allow_zero=True)
    fragment_count = _positive_int(data.get("fragment_count"))
    determinate = total is not None or percent_value is not None or (
        fragment_index is not None and fragment_count is not None
    )
    if downloaded is not None and total:
        percent = int(downloaded * 100 / total)
    elif percent_value is not None:
        percent = int(percent_value)
    elif fragment_index is not None and fragment_count:
        percent = int(fragment_index * 100 / fragment_count)
    else:
        percent = 0
    return Progress(
        job_id=job.id,
        stage=JobStage.DOWNLOADING,
        percent=max(0, min(99, percent)),
        attempt=job.attempt,
        item=item,
        item_count=item_count,
        downloaded_bytes=downloaded,
        total_bytes=total,
        total_bytes_is_estimate=exact_total is None and estimated_total is not None,
        speed_bytes_per_second=_positive_int(data.get("speed")),
        eta_seconds=_positive_int(data.get("eta"), allow_zero=True),
        elapsed_seconds=_positive_int(data.get("elapsed"), allow_zero=True),
        indeterminate=not determinate,
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _positive_int(value: object, *, allow_zero: bool = False) -> int | None:
    number = _number(value)
    if number is None or number < 0 or (number == 0 and not allow_zero):
        return None
    return int(number)


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


def _can_retry_mirror(exc: httpx.HTTPError) -> bool:
    if isinstance(exc, httpx.RequestError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code in {403, 404, 410, 429, 451}
        or exc.response.status_code >= 500
    )


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
            "-movflags",
            "+faststart",
            str(partial),
        )
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    started_at = time.monotonic()
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
        elapsed = max(0.001, time.monotonic() - started_at)
        downloaded = partial.stat().st_size if partial.exists() else 0
        await progress(
            Progress(
                job_id=job.id,
                stage=JobStage.DOWNLOADING,
                percent=0,
                attempt=job.attempt,
                item=item,
                item_count=item_count,
                downloaded_bytes=downloaded,
                speed_bytes_per_second=int(downloaded / elapsed) or None,
                elapsed_seconds=int(elapsed),
                indeterminate=True,
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


async def _split_audio_chapter(
    source: Path,
    chapter: MediaChapter,
    index: int,
    author: str | None,
    thumbnail_path: PurePosixPath | None,
    max_file_size: int,
    cancellation,
) -> DownloadArtifact:
    filename = build_audio_filename(chapter.title, author, suffix=source.suffix)
    target = source.with_name(f"{index:03d}-{filename}")
    container_args = (
        ("-movflags", "+faststart")
        if target.suffix.lower() in {".m4a", ".mp4"}
        else ()
    )
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss",
        f"{chapter.start_ms / 1_000:.3f}",
        "-i",
        str(source),
        "-t",
        f"{(chapter.end_ms - chapter.start_ms) / 1_000:.3f}",
        "-map",
        "0:a:0",
        "-map",
        "0:v:0?",
        "-codec",
        "copy",
        "-map_metadata",
        "0",
        "-metadata",
        f"title={chapter.title}",
        "-metadata",
        f"artist={author or ''}",
        *container_args,
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    while process.returncode is None:
        if await cancellation.requested():
            process.terminate()
            await process.wait()
            target.unlink(missing_ok=True)
            raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
        try:
            await asyncio.wait_for(process.wait(), timeout=0.25)
        except TimeoutError:
            continue
    if process.returncode:
        detail = (
            (await process.stderr.read()).decode(errors="replace")[-1_000:]
            if process.stderr is not None
            else ""
        )
        target.unlink(missing_ok=True)
        raise DownloadError(
            ErrorCode.PROVIDER_FAILURE,
            detail or "Chapter splitting failed",
            retryable=False,
        )
    size = target.stat().st_size
    if size > max_file_size:
        target.unlink(missing_ok=True)
        raise DownloadError(
            ErrorCode.TOO_LARGE, "Prepared chapter exceeds the configured size limit"
        )
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return DownloadArtifact(
        path=PurePosixPath(target),
        kind=MediaKind.AUDIO,
        size=size,
        checksum=digest.hexdigest(),
        mime_type=mimetypes.guess_type(target.name)[0],
        title=chapter.title,
        author=author,
        duration_ms=chapter.end_ms - chapter.start_ms,
        thumbnail_path=thumbnail_path,
    )


async def _transcode_audio(
    target: Path,
    cancellation,
    *,
    title: str | None = None,
    author: str | None = None,
    cover: Path | None = None,
    report=None,
    strategy: str | None = None,
) -> Path:
    strategy = strategy or await _audio_strategy(target)
    if strategy == "passthrough" and cover is None:
        return target
    if strategy == "rename_mp3" and cover is None:
        final = target.with_suffix(".mp3")
        target.replace(final)
        return final
    output_suffix = (
        ".mp3"
        if strategy in {"rename_mp3", "remux_mp3"}
        else target.suffix
        if strategy == "passthrough"
        else ".m4a"
    )
    output = target.with_name(f"{target.stem}.converted{output_suffix}")
    final = target.with_suffix(output_suffix)
    codec_args = (
        ("-codec:a", "copy")
        if strategy in {"passthrough", "rename_mp3", "remux", "remux_mp3"}
        else (
            "-codec:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-aac_coder",
            "fast",
        )
    )
    cover_input = ("-i", str(cover)) if cover else ()
    stream_args = (
        (
            "-map",
            "0:a:0",
            "-map",
            "1:v:0",
            "-codec:v",
            "mjpeg",
            "-disposition:v:0",
            "attached_pic",
        )
        if cover
        else ("-vn", "-map", "0:a:0")
    )
    container_args = ("-id3v2_version", "3") if output_suffix == ".mp3" else ()
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(target),
        *cover_input,
        *stream_args,
        *codec_args,
        "-map_metadata",
        "0",
        "-metadata",
        f"title={title or ''}",
        "-metadata",
        f"artist={author or ''}",
        *container_args,
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    started_at = time.monotonic()
    while process.returncode is None:
        if await cancellation.requested():
            process.terminate()
            await process.wait()
            output.unlink(missing_ok=True)
            raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
        if report is not None:
            await report(max(0, int(time.monotonic() - started_at)))
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
        if strategy in {"passthrough", "rename_mp3", "remux", "remux_mp3"}:
            return await _transcode_audio(
                target,
                cancellation,
                title=title,
                author=author,
                cover=cover,
                report=report,
                strategy="transcode",
            )
        raise DownloadError(
            ErrorCode.PROVIDER_FAILURE,
            detail or "Audio conversion failed",
            retryable=False,
        )
    target.unlink(missing_ok=True)
    output.replace(final)
    return final


async def _audio_strategy(target: Path) -> str:
    """Choose the cheapest conversion that works in Telegram's music player."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name:format=format_name",
            "-of",
            "json",
            str(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode:
            return "transcode"
        probe = json.loads(stdout)
    except (OSError, json.JSONDecodeError, TypeError):
        return "transcode"

    streams = probe.get("streams")
    if not isinstance(streams, list) or not streams:
        return "transcode"
    first = streams[0]
    codec = first.get("codec_name") if isinstance(first, dict) else None
    formats = set(str(probe.get("format", {}).get("format_name", "")).split(","))
    suffix = target.suffix.lower()
    if codec == "aac" and suffix == ".m4a" and formats & {"mov", "mp4", "m4a"}:
        return "passthrough"
    if codec == "mp3" and suffix == ".mp3" and "mp3" in formats:
        return "passthrough"
    if codec == "mp3" and "mp3" in formats:
        return "rename_mp3"
    if codec == "mp3":
        return "remux_mp3"
    if codec == "aac":
        return "remux"
    return "transcode"


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


async def _transcode_video(
    target: Path, cancellation, *, report=None, strategy: str | None = None
) -> Path:
    output = target.with_name(f"{target.stem}.converted.mp4")
    final = target.with_suffix(".mp4")
    strategy = strategy or await _video_strategy(target)
    if strategy == "passthrough":
        return target
    codec_args: tuple[str, ...] = (
        ("-codec", "copy")
        if strategy == "remux"
        else (
            "-codec:v",
            "copy",
            "-codec:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-aac_coder",
            "fast",
        )
        if strategy == "transcode_audio"
        else (
            "-codec:v",
            "libx264",
            "-preset",
            "veryfast",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
            "-crf",
            "23",
            "-codec:a",
            "copy",
        )
        if strategy == "transcode_video"
        else (
            "-codec:v",
            "libx264",
            "-preset",
            "veryfast",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
            "-crf",
            "23",
            "-codec:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-aac_coder",
            "fast",
        )
    )
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(target),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        *codec_args,
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    started_at = time.monotonic()
    while process.returncode is None:
        if await cancellation.requested():
            process.terminate()
            await process.wait()
            output.unlink(missing_ok=True)
            raise DownloadError(ErrorCode.CANCELLED, "Download cancelled")
        if report is not None:
            await report(max(0, int(time.monotonic() - started_at)))
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
        fallback = {
            "remux": "transcode_audio",
            "transcode_audio": "transcode",
            "transcode_video": "transcode",
        }.get(strategy)
        if fallback is not None:
            return await _transcode_video(
                target,
                cancellation,
                report=report,
                strategy=fallback,
            )
        raise DownloadError(
            ErrorCode.PROVIDER_FAILURE,
            detail or "Video conversion failed",
            retryable=False,
        )
    target.unlink(missing_ok=True)
    output.replace(final)
    return final


async def _is_telegram_compatible_video(target: Path) -> bool:
    """Return whether an MP4 can be prepared with a metadata-only remux."""
    return await _video_strategy(target) in {"passthrough", "remux"}


async def _video_strategy(target: Path) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,pix_fmt",
            "-of",
            "json",
            str(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if process.returncode:
            return "transcode"
        probe = json.loads(stdout)
    except (OSError, json.JSONDecodeError, TypeError):
        return "transcode"

    formats = set(str(probe.get("format", {}).get("format_name", "")).split(","))
    streams = probe.get("streams")
    if not isinstance(streams, list):
        return "transcode"
    video_stream = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    audio_stream = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ),
        None,
    )
    video_compatible = (
        video_stream is not None
        and video_stream.get("codec_name") == "h264"
        and video_stream.get("pix_fmt") in {"yuv420p", "yuvj420p"}
    )
    audio_compatible = audio_stream is None or audio_stream.get("codec_name") == "aac"
    if video_compatible and audio_compatible:
        if (
            bool(formats & {"mp4", "mov"})
            and target.suffix.lower() == ".mp4"
            and _mp4_is_faststart(target)
        ):
            return "passthrough"
        return "remux"
    if video_compatible:
        return "transcode_audio"
    if audio_compatible:
        return "transcode_video"
    return "transcode"


def _mp4_is_faststart(target: Path) -> bool:
    """Return whether the MP4 metadata atom appears before media payload."""
    try:
        with target.open("rb") as source:
            while header := source.read(8):
                if len(header) != 8:
                    return False
                size = int.from_bytes(header[:4], "big")
                atom = header[4:]
                header_size = 8
                if size == 1:
                    extended = source.read(8)
                    if len(extended) != 8:
                        return False
                    size = int.from_bytes(extended, "big")
                    header_size = 16
                if atom == b"moov":
                    return True
                if atom == b"mdat" or size < header_size:
                    return False
                source.seek(size - header_size, 1)
    except OSError:
        return False
    return False
