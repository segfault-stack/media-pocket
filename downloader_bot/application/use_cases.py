from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from downloader_bot.domain import (
    DeliveryMode,
    ErrorCode,
    Job,
    JobKind,
    JobStage,
    Progress,
    SelectionMode,
    SelectionRequest,
    UserPreferences,
)
from downloader_bot.domain.errors import DownloadError

from .ports import (
    AnalyticsRepository,
    ArtifactStore,
    Clock,
    DownloadEngine,
    IdGenerator,
    JobQueue,
    JobRepository,
    MediaCacheRepository,
    PlatformRegistry,
    ProgressBus,
    SelectionRepository,
    SettingsRepository,
    TelegramGateway,
    UserRepository,
)


@dataclass(frozen=True, slots=True)
class SubmitDownloadCommand:
    user_id: int
    chat_id: int
    url: str
    kind: JobKind = JobKind.DIRECT
    parent_id: str | None = None
    source_message_id: int | None = None
    business_connection_id: str | None = None
    audio_only: bool = False
    quality: str | None = None
    document_mode: bool | None = None
    status_message_id: int | None = None
    inline_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubmissionPlan:
    audio_only_by_url: tuple[bool, ...]
    ask_for_youtube: bool = False


class PlanSubmission:
    """Choose immediate provider-native modes without creating persistent state."""

    def __init__(
        self, settings: SettingsRepository, registry: PlatformRegistry
    ) -> None:
        self._settings = settings
        self._registry = registry

    async def execute(
        self, user_id: int, urls: tuple[str, ...]
    ) -> SubmissionPlan:
        preferences = await self._settings.get(user_id)
        youtube_mode = _youtube_mode(preferences)
        platforms = tuple(self._registry.detect(_validated_url(url)).platform for url in urls)
        return SubmissionPlan(
            audio_only_by_url=tuple(
                platform.value in _AUDIO_PLATFORMS
                or (platform.value == "youtube" and youtube_mode == "audio")
                for platform in platforms
            ),
            ask_for_youtube=youtube_mode == "ask"
            and any(platform.value == "youtube" for platform in platforms),
        )


class SubmitDownload:
    def __init__(
        self,
        jobs: JobRepository,
        users: UserRepository,
        ids: IdGenerator,
        clock: Clock,
        analytics: AnalyticsRepository,
        settings: SettingsRepository,
    ) -> None:
        self._jobs = jobs
        self._users = users
        self._ids = ids
        self._clock = clock
        self._analytics = analytics
        self._settings = settings

    async def execute(self, command: SubmitDownloadCommand) -> tuple[Job, bool]:
        job = await self.prepare(command)
        persisted, created = await self._jobs.create_with_outbox(job)
        await self._analytics.record(
            "job_submitted" if created else "job_deduplicated",
            user_id=command.user_id,
            job_id=persisted.id,
        )
        return persisted, created

    async def prepare(
        self, command: SubmitDownloadCommand, *, dedupe_scope: str | None = None
    ) -> Job:
        url = _validated_url(command.url)
        await self._users.ensure(command.user_id)
        preferences = await self._settings.get(command.user_id)
        if command.quality is not None:
            preferences = replace(preferences, quality=command.quality)
        if command.document_mode is not None:
            preferences = replace(preferences, document_mode=command.document_mode)
        variant = preferences.cache_variant(audio_only=command.audio_only)
        dedupe_key = hashlib.sha256(
            f"{command.user_id}\0{url}\0{variant}\0{dedupe_scope or ''}".encode()
        ).hexdigest()
        now = self._clock.now()
        job = Job(
            id=self._ids.new(),
            user_id=command.user_id,
            chat_id=command.chat_id,
            source_url=url,
            dedupe_key=dedupe_key,
            kind=command.kind,
            parent_id=command.parent_id,
            source_message_id=command.source_message_id,
            business_connection_id=command.business_connection_id,
            status_message_id=command.status_message_id,
            inline_message_id=command.inline_message_id,
            audio_only=command.audio_only,
            preferences=preferences,
            created_at=now,
            updated_at=now,
        )
        return job


class CancelDownload:
    def __init__(self, jobs: JobRepository, analytics: AnalyticsRepository) -> None:
        self._jobs = jobs
        self._analytics = analytics

    async def execute(self, job_id: str, user_id: int) -> Job | None:
        job = await self._jobs.request_cancel(job_id, user_id)
        if job is not None:
            await self._analytics.record(
                "job_cancel_requested", user_id=user_id, job_id=job.id
            )
            if job.stage is JobStage.QUEUED:
                cancelling = await self._jobs.transition(
                    job.id,
                    {JobStage.QUEUED},
                    JobStage.CANCELLING,
                    cancel_requested=True,
                )
                if cancelling is not None:
                    job = (
                        await self._jobs.transition(
                            job.id,
                            {JobStage.CANCELLING},
                            JobStage.CANCELLED,
                            error_code=ErrorCode.CANCELLED,
                        )
                        or cancelling
                    )
            if job.is_parent:
                await self._jobs.request_cancel_children(job.id, user_id)
        return job


class SubmitBatch:
    def __init__(
        self,
        submit: SubmitDownload,
        jobs: JobRepository,
        ids: IdGenerator,
        clock: Clock,
        settings: SettingsRepository,
    ) -> None:
        self._submit = submit
        self._jobs = jobs
        self._ids = ids
        self._clock = clock
        self._settings = settings

    async def execute(
        self,
        *,
        user_id: int,
        chat_id: int,
        urls: tuple[str, ...],
        source_message_id: int | None = None,
        business_connection_id: str | None = None,
        audio_only: bool = False,
        audio_only_by_url: tuple[bool, ...] | None = None,
        quality: str | None = None,
        document_mode: bool | None = None,
        status_message_id: int | None = None,
    ) -> tuple[Job, tuple[Job, ...]]:
        if not urls:
            raise ValueError("batch requires at least one URL")
        if audio_only_by_url is not None and len(audio_only_by_url) != len(urls):
            raise ValueError("batch modes must match URLs")
        now = self._clock.now()
        parent_id = self._ids.new()
        preferences = await self._settings.get(user_id)
        if quality is not None:
            preferences = replace(preferences, quality=quality)
        if document_mode is not None:
            preferences = replace(preferences, document_mode=document_mode)
        parent = Job(
            id=parent_id,
            user_id=user_id,
            chat_id=chat_id,
            source_url="batch://links",
            dedupe_key=f"batch:{parent_id}",
            kind=JobKind.BATCH,
            is_parent=True,
            children_total=len(urls),
            source_message_id=source_message_id,
            business_connection_id=business_connection_id,
            status_message_id=status_message_id,
            preferences=preferences,
            created_at=now,
            updated_at=now,
        )
        children: list[Job] = []
        for index, url in enumerate(urls):
            child = await self._submit.prepare(
                SubmitDownloadCommand(
                    user_id=user_id,
                    chat_id=chat_id,
                    url=url,
                    parent_id=parent.id,
                    source_message_id=source_message_id,
                    business_connection_id=business_connection_id,
                    audio_only=audio_only_by_url[index]
                    if audio_only_by_url is not None
                    else audio_only,
                    quality=quality,
                    document_mode=document_mode,
                    status_message_id=None,
                ),
                dedupe_scope=f"{parent.id}:{index}",
            )
            children.append(child)
        await self._jobs.create_batch(parent, tuple(children))
        return parent, tuple(children)


_AUDIO_PLATFORMS = frozenset({"spotify", "soundcloud", "hitmoz", "zaycev"})
_SOCIAL_MEDIA_PLATFORMS = frozenset(
    {"tiktok", "instagram", "x", "threads", "pinterest"}
)
_VIDEO_QUALITIES = frozenset({"best", "1080", "720", "480"})


class CreateSelection:
    def __init__(
        self,
        selections: SelectionRepository,
        users: UserRepository,
        settings: SettingsRepository,
        registry: PlatformRegistry,
        ids: IdGenerator,
        clock: Clock,
        analytics: AnalyticsRepository,
    ) -> None:
        self._selections = selections
        self._users = users
        self._settings = settings
        self._registry = registry
        self._ids = ids
        self._clock = clock
        self._analytics = analytics

    async def execute(
        self,
        *,
        user_id: int,
        chat_id: int,
        urls: tuple[str, ...],
        kind: JobKind = JobKind.DIRECT,
        source_message_id: int | None = None,
        business_connection_id: str | None = None,
    ) -> SelectionRequest:
        if not urls:
            raise ValueError("selection requires at least one URL")
        validated = tuple(_validated_url(url) for url in urls)
        await self._users.ensure(user_id)
        preferences = await self._settings.get(user_id)
        platforms = tuple(self._registry.detect(url).platform for url in validated)
        mode = _default_mode(platforms, preferences)
        now = self._clock.now()
        selection = await self._selections.create(
            SelectionRequest(
                token=self._ids.new(),
                user_id=user_id,
                chat_id=chat_id,
                urls=validated,
                platforms=platforms,
                mode=mode,
                quality="best"
                if any(
                    platform.value
                    in _SOCIAL_MEDIA_PLATFORMS | _AUDIO_PLATFORMS
                    for platform in platforms
                )
                else preferences.quality,
                delivery=DeliveryMode.FILE
                if preferences.document_mode
                else DeliveryMode.MEDIA,
                source_message_id=source_message_id,
                business_connection_id=business_connection_id,
                created_at=now,
                expires_at=now + timedelta(minutes=15),
                job_kind=kind,
            )
        )
        await self._analytics.record("selection_shown", user_id=user_id)
        return selection

    async def bind_message(
        self, token: str, user_id: int, message_id: int
    ) -> SelectionRequest | None:
        return await self._selections.update(
            token, user_id, self._clock.now(), status_message_id=message_id
        )


class UpdateSelection:
    def __init__(
        self,
        selections: SelectionRepository,
        clock: Clock,
        analytics: AnalyticsRepository,
    ) -> None:
        self._selections = selections
        self._clock = clock
        self._analytics = analytics

    async def get(self, token: str) -> SelectionRequest | None:
        return await self._selections.get(token)

    async def record_expired(self, user_id: int) -> None:
        await self._analytics.record("selection_expired", user_id=user_id)

    async def execute(
        self, token: str, user_id: int, *, action: str, value: str
    ) -> SelectionRequest | None:
        selection = await self._selections.get(token)
        now = self._clock.now()
        if (
            selection is None
            or selection.user_id != user_id
            or not selection.active
            or selection.expires_at <= now
        ):
            return None
        changes: dict[str, object]
        if action == "mode" and value in {item.value for item in SelectionMode}:
            requested = SelectionMode(value)
            changes = {"mode": _allowed_mode(requested, selection.platforms)}
        elif action == "quality" and value in _VIDEO_QUALITIES:
            changes = {"quality": value}
        elif action == "delivery" and value in {item.value for item in DeliveryMode}:
            changes = {"delivery": DeliveryMode(value)}
        else:
            raise ValueError("invalid selection option")
        updated = await self._selections.update(token, user_id, now, **changes)
        if updated:
            await self._analytics.record("format_selected", user_id=user_id)
        return updated

    async def cancel(self, token: str, user_id: int) -> SelectionRequest | None:
        result = await self._selections.cancel(token, user_id, self._clock.now())
        if result:
            await self._analytics.record("selection_cancelled", user_id=user_id)
        return result


class ConfirmSelection:
    def __init__(
        self,
        selections: SelectionRepository,
        submit: SubmitDownload,
        submit_batch: SubmitBatch,
        clock: Clock,
        analytics: AnalyticsRepository,
    ) -> None:
        self._selections = selections
        self._submit = submit
        self._submit_batch = submit_batch
        self._clock = clock
        self._analytics = analytics

    async def execute(
        self, token: str, user_id: int, *, inline_message_id: str | None = None
    ) -> tuple[SelectionRequest | None, Job | None, bool]:
        now = self._clock.now()
        selection = await self._selections.claim(token, user_id, now)
        if selection is None:
            return await self._selections.get(token), None, False
        audio_only = selection.mode is SelectionMode.AUDIO
        document_mode = selection.delivery is DeliveryMode.FILE
        try:
            if len(selection.urls) > 1:
                job, _ = await self._submit_batch.execute(
                    user_id=selection.user_id,
                    chat_id=selection.chat_id,
                    urls=selection.urls,
                    source_message_id=selection.source_message_id,
                    business_connection_id=selection.business_connection_id,
                    audio_only=audio_only,
                    quality=selection.quality,
                    document_mode=document_mode,
                    status_message_id=selection.status_message_id,
                )
            else:
                job, _ = await self._submit.execute(
                    SubmitDownloadCommand(
                        user_id=selection.user_id,
                        chat_id=selection.chat_id,
                        url=selection.urls[0],
                        kind=selection.job_kind,
                        source_message_id=selection.source_message_id,
                        business_connection_id=selection.business_connection_id,
                        audio_only=audio_only,
                        quality=selection.quality,
                        document_mode=document_mode,
                        status_message_id=selection.status_message_id,
                        inline_message_id=inline_message_id,
                    )
                )
        except Exception:
            await self._selections.release_claim(token, user_id, now)
            raise
        await self._analytics.record(
            "download_confirmed", user_id=user_id, job_id=job.id
        )
        return selection, job, True


class GetUserJobs:
    def __init__(self, jobs: JobRepository) -> None:
        self._jobs = jobs

    async def execute(self, user_id: int, *, limit: int = 10) -> tuple[Job, ...]:
        return await self._jobs.recent_for_user(user_id, limit=limit)


class RetryInFormat:
    def __init__(
        self,
        jobs: JobRepository,
        submit: SubmitDownload,
        analytics: AnalyticsRepository,
    ) -> None:
        self._jobs = jobs
        self._submit = submit
        self._analytics = analytics

    async def execute(self, job_id: str, user_id: int, action: str) -> Job | None:
        source = await self._jobs.get(job_id)
        if source is None or source.user_id != user_id:
            return None
        audio_only = source.audio_only
        document_mode = source.preferences.document_mode
        if action == "audio":
            audio_only = True
        elif action == "video":
            audio_only = False
        elif action == "file":
            document_mode = True
        elif action != "retry":
            return None
        job, _ = await self._submit.execute(
            SubmitDownloadCommand(
                user_id=source.user_id,
                chat_id=source.chat_id,
                url=source.source_url,
                kind=source.kind
                if source.kind is not JobKind.BATCH
                else JobKind.DIRECT,
                source_message_id=source.source_message_id,
                business_connection_id=source.business_connection_id,
                audio_only=audio_only,
                quality=source.preferences.quality,
                document_mode=document_mode,
            )
        )
        await self._analytics.record(
            "result_action_used", user_id=user_id, job_id=job.id
        )
        return job


class RepositoryCancellation:
    def __init__(self, jobs: JobRepository, job_id: str) -> None:
        self._jobs = jobs
        self._job_id = job_id

    async def requested(self) -> bool:
        job = await self._jobs.get(self._job_id)
        return (
            job is None
            or job.cancel_requested
            or job.stage in {JobStage.CANCELLING, JobStage.CANCELLED}
        )


class ProcessDownload:
    def __init__(
        self,
        jobs: JobRepository,
        registry: PlatformRegistry,
        engine: DownloadEngine,
        artifacts: ArtifactStore,
        progress: ProgressBus,
        analytics: AnalyticsRepository,
        clock: Clock,
        cache: MediaCacheRepository,
    ) -> None:
        self._jobs = jobs
        self._registry = registry
        self._engine = engine
        self._artifacts = artifacts
        self._progress = progress
        self._analytics = analytics
        self._clock = clock
        self._cache = cache

    async def execute(self, job_id: str) -> Job | None:
        job = await self._jobs.get(job_id)
        if job is None or job.terminal:
            return job
        if job.cancel_requested:
            return await self._cancel(job)
        original_job = job
        try:
            job = await self._move(
                job, {JobStage.QUEUED, JobStage.RETRYING}, JobStage.RESOLVING, 0
            )
            if job is None:
                return await self._jobs.get(job_id)
            adapter = self._registry.detect(job.source_url)
            cached = await self._cache.get(job.cache_key)
            if cached:
                await self._artifacts.persist(job.id, cached)
                job = await self._move(
                    job, {JobStage.RESOLVING}, JobStage.PROCESSING, 100
                )
                if job is None:
                    return await self._jobs.get(job_id)
                job = await self._move(job, {JobStage.PROCESSING}, JobStage.READY, 100)
                if job:
                    await self._analytics.record(
                        "job_ready", user_id=job.user_id, job_id=job.id
                    )
                return job
            post = await adapter.resolve(
                job.source_url, job.preferences, audio_only=job.audio_only
            )
            if await RepositoryCancellation(self._jobs, job.id).requested():
                return await self._cancel(job)
            job = await self._move(job, {JobStage.RESOLVING}, JobStage.DOWNLOADING, 0)
            if job is None:
                return await self._jobs.get(job_id)

            async def report(event: Progress) -> None:
                await self._progress.publish(event)

            cancellation = RepositoryCancellation(self._jobs, job.id)
            produced = await self._engine.download(post, job, report, cancellation)
            if await cancellation.requested():
                return await self._cancel(job)
            job = await self._move(
                job, {JobStage.DOWNLOADING}, JobStage.PROCESSING, 100
            )
            if job is None:
                return await self._jobs.get(job_id)
            await self._artifacts.persist(job.id, produced)
            job = await self._move(job, {JobStage.PROCESSING}, JobStage.READY, 100)
            if job is None:
                return await self._jobs.get(job_id)
            await self._cache.put(job.cache_key, produced)
            await self._analytics.record(
                "job_ready", user_id=job.user_id, job_id=job.id
            )
            return job
        except DownloadError as exc:
            if exc.code is ErrorCode.CANCELLED:
                return await self._cancel(job or original_job)
            return await self._handle_error(job or original_job, exc)
        except Exception as exc:  # noqa: BLE001 - normalize unknown provider/transport failures at the use-case boundary
            return await self._handle_error(
                job or original_job,
                DownloadError(ErrorCode.PROVIDER_FAILURE, str(exc), retryable=True),
            )

    async def _move(
        self, job: Job, expected: set[JobStage], stage: JobStage, percent: int
    ) -> Job | None:
        moved = await self._jobs.transition(job.id, expected, stage)
        if moved:
            await self._progress.publish(
                Progress(
                    job_id=job.id, stage=stage, percent=percent, attempt=moved.attempt
                )
            )
        return moved

    async def _cancel(self, job: Job) -> Job | None:
        cancelling = await self._jobs.transition(
            job.id,
            {
                JobStage.QUEUED,
                JobStage.RESOLVING,
                JobStage.DOWNLOADING,
                JobStage.PROCESSING,
                JobStage.READY,
                JobStage.RETRYING,
            },
            JobStage.CANCELLING,
            cancel_requested=True,
        )
        if cancelling is None:
            return await self._jobs.get(job.id)
        await self._artifacts.cleanup(job.id)
        cancelled = await self._jobs.transition(
            job.id,
            {JobStage.CANCELLING},
            JobStage.CANCELLED,
            error_code=ErrorCode.CANCELLED,
        )
        if cancelled:
            await self._progress.publish(
                Progress(job_id=job.id, stage=JobStage.CANCELLED)
            )
        return cancelled

    async def _handle_error(self, job: Job, error: DownloadError) -> Job | None:
        retry_limit = {
            ErrorCode.RATE_LIMITED: 5,
            ErrorCode.PROVIDER_FAILURE: 3,
            ErrorCode.UNAVAILABLE: 2,
        }.get(error.code, 1)
        if error.retryable and job.attempt < retry_limit and not job.cancel_requested:
            retrying = await self._jobs.transition(
                job.id,
                {JobStage.RESOLVING, JobStage.DOWNLOADING, JobStage.PROCESSING},
                JobStage.RETRYING,
                attempt=job.attempt + 1,
                error_code=error.code,
                error_detail=error.message,
            )
            if retrying:
                await self._progress.publish(
                    Progress(
                        job_id=job.id,
                        stage=JobStage.RETRYING,
                        attempt=retrying.attempt,
                        attempt_limit=retry_limit,
                        detail=error.message,
                        error_code=error.code,
                    )
                )
            return retrying
        failed = await self._jobs.transition(
            job.id,
            {
                JobStage.QUEUED,
                JobStage.RESOLVING,
                JobStage.DOWNLOADING,
                JobStage.PROCESSING,
                JobStage.RETRYING,
            },
            JobStage.FAILED,
            error_code=error.code,
            error_detail=error.message,
        )
        if failed:
            await self._analytics.record(
                "job_failed", user_id=failed.user_id, job_id=failed.id
            )
            await self._progress.publish(
                Progress(
                    job_id=job.id,
                    stage=JobStage.FAILED,
                    detail=error.message,
                    error_code=error.code,
                )
            )
        return failed


class DeliverResult:
    def __init__(
        self,
        jobs: JobRepository,
        artifacts: ArtifactStore,
        telegram: TelegramGateway,
        analytics: AnalyticsRepository,
        progress: ProgressBus,
    ) -> None:
        self._jobs = jobs
        self._artifacts = artifacts
        self._telegram = telegram
        self._analytics = analytics
        self._progress = progress

    async def execute(self, job_id: str, *, manual_retry: bool = False) -> Job | None:
        job = await self._jobs.get(job_id)
        if job is None:
            return None
        if job.kind is JobKind.INLINE and not job.inline_message_id:
            return job
        if job.stage is JobStage.DELIVERING and not manual_retry:
            await self._telegram.show_manual_retry(job)
            return job
        if job.stage is not JobStage.READY and not (
            manual_retry and job.stage is JobStage.DELIVERING
        ):
            return job
        if job.stage is JobStage.READY:
            claimed = await self._jobs.transition(
                job.id, {JobStage.READY}, JobStage.DELIVERING
            )
            if claimed is None:
                return await self._jobs.get(job.id)
            job = claimed
            await self._progress.publish(
                Progress(
                    job_id=job.id,
                    stage=JobStage.DELIVERING,
                    percent=100,
                    attempt=job.attempt,
                )
            )
        if job.cancel_requested:
            return job
        delivered_to_user = await self._telegram.deliver(
            job,
            await self._artifacts.get(job.id),
            job.preferences,
        )
        if delivered_to_user is False:
            return job
        delivered = await self._jobs.transition(
            job.id, {JobStage.DELIVERING}, JobStage.DELIVERED
        )
        if delivered:
            await self._analytics.record(
                "job_delivered", user_id=job.user_id, job_id=job.id
            )
            await self._progress.publish(
                Progress(
                    job_id=job.id,
                    stage=JobStage.DELIVERED,
                    percent=100,
                    attempt=job.attempt,
                )
            )
        return delivered


class BindInlineResult:
    def __init__(self, jobs: JobRepository) -> None:
        self._jobs = jobs

    async def execute(
        self, job_id: str, user_id: int, inline_message_id: str
    ) -> Job | None:
        return await self._jobs.bind_inline(job_id, user_id, inline_message_id)


class CustomizeJob:
    def __init__(self, jobs: JobRepository) -> None:
        self._jobs = jobs

    async def audio(self, job_id: str, user_id: int) -> Job | None:
        return await self._jobs.customize(job_id, user_id, audio_only=True)

    async def document(self, job_id: str, user_id: int) -> Job | None:
        return await self._jobs.customize(job_id, user_id, document_mode=True)


class RefreshParent:
    def __init__(self, jobs: JobRepository, progress: ProgressBus) -> None:
        self._jobs = jobs
        self._progress = progress

    async def execute(self, parent_id: str) -> Job | None:
        parent = await self._jobs.get(parent_id)
        children = await self._jobs.children(parent_id)
        if parent is None or not children:
            return parent
        terminal = [child for child in children if child.terminal]
        delivered = [child for child in children if child.stage is JobStage.DELIVERED]
        percent = int(
            sum(_job_percent(child.stage) for child in children) / len(children)
        )
        if len(terminal) == len(children):
            if len(delivered) == len(children):
                stage = JobStage.DELIVERED
            elif all(child.stage is JobStage.CANCELLED for child in children):
                stage = JobStage.CANCELLED
            else:
                stage = JobStage.FAILED
        elif any(
            child.stage in {JobStage.READY, JobStage.DELIVERING} for child in children
        ):
            stage = JobStage.PROCESSING
        elif any(
            child.stage in {JobStage.RESOLVING, JobStage.DOWNLOADING, JobStage.RETRYING}
            for child in children
        ):
            stage = JobStage.DOWNLOADING
        else:
            stage = JobStage.QUEUED
        updated = await self._jobs.transition(parent.id, {parent.stage}, stage)
        current = updated or await self._jobs.get(parent.id)
        if current:
            await self._progress.publish(
                Progress(
                    job_id=current.id,
                    stage=current.stage,
                    percent=percent,
                    item=len(terminal),
                    item_count=len(children),
                )
            )
        return current


class CleanupArtifacts:
    def __init__(
        self, jobs: JobRepository, artifacts: ArtifactStore, clock: Clock
    ) -> None:
        self._jobs = jobs
        self._artifacts = artifacts
        self._clock = clock

    async def execute(self, *, retention_seconds: int, limit: int = 100) -> int:
        from datetime import timedelta

        now = self._clock.now()
        before = now - timedelta(seconds=max(0, retention_seconds))
        job_ids = await self._jobs.expired_artifacts(before, limit)
        for job_id in job_ids:
            await self._artifacts.cleanup(job_id)
            await self._jobs.mark_artifacts_cleaned(job_id, now)
        return len(job_ids)


class ManageSettings:
    def __init__(self, settings: SettingsRepository) -> None:
        self._settings = settings

    async def get(self, user_id: int) -> UserPreferences:
        return await self._settings.get(user_id)

    async def update(self, user_id: int, **changes: object) -> UserPreferences:
        current = await self._settings.get(user_id)
        allowed = set(UserPreferences.__dataclass_fields__)
        if unknown := set(changes) - allowed:
            raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
        if "youtube_mode" in changes:
            youtube_mode = changes["youtube_mode"]
            if youtube_mode not in {"video", "audio", "ask"}:
                raise ValueError("invalid YouTube mode")
            changes["default_audio_only"] = youtube_mode == "audio"
        elif "default_audio_only" in changes:
            changes["youtube_mode"] = (
                "audio" if changes["default_audio_only"] else "video"
            )
        return await self._settings.save(
            user_id, replace(current, **cast(Any, changes))
        )


class ProcessInline:
    def __init__(self, submit: SubmitDownload) -> None:
        self._submit = submit

    async def execute(self, *, user_id: int, url: str) -> tuple[Job, bool]:
        return await self._submit.execute(
            SubmitDownloadCommand(
                user_id=user_id,
                chat_id=user_id,
                url=url,
                kind=JobKind.INLINE,
            )
        )


class GetStats:
    def __init__(self, analytics: AnalyticsRepository) -> None:
        self._analytics = analytics

    async def execute(self) -> dict[str, int]:
        return await self._analytics.stats()


class PublishOutbox:
    def __init__(self, jobs: JobRepository, queue: JobQueue) -> None:
        self._jobs = jobs
        self._queue = queue

    async def execute(self, limit: int = 100) -> int:
        events = await self._jobs.claim_outbox(limit)
        for event_id, job_id in events:
            await self._queue.publish(job_id)
            await self._jobs.mark_outbox_published(event_id)
        return len(events)


def _validated_url(value: str) -> str:
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DownloadError(ErrorCode.UNSUPPORTED, "A valid HTTP(S) URL is required")
    return url


def _job_percent(stage: JobStage) -> int:
    return {
        JobStage.QUEUED: 0,
        JobStage.RESOLVING: 10,
        JobStage.DOWNLOADING: 50,
        JobStage.RETRYING: 50,
        JobStage.PROCESSING: 90,
        JobStage.READY: 95,
        JobStage.DELIVERING: 98,
        JobStage.DELIVERED: 100,
        JobStage.CANCELLING: 50,
        JobStage.CANCELLED: 100,
        JobStage.FAILED: 100,
    }[stage]


def _default_mode(platforms, preferences: UserPreferences) -> SelectionMode:
    if all(platform.value in _AUDIO_PLATFORMS for platform in platforms):
        return SelectionMode.AUDIO
    if _youtube_mode(preferences) == "audio":
        return _allowed_mode(SelectionMode.AUDIO, platforms)
    if any(platform.value in _SOCIAL_MEDIA_PLATFORMS for platform in platforms):
        return SelectionMode.MEDIA
    return SelectionMode.VIDEO


def _youtube_mode(preferences: UserPreferences) -> str:
    if preferences.youtube_mode in {"audio", "ask"}:
        return preferences.youtube_mode
    return "audio" if preferences.default_audio_only else "video"


def _allowed_mode(requested: SelectionMode, platforms) -> SelectionMode:
    if all(platform.value in _AUDIO_PLATFORMS for platform in platforms):
        return SelectionMode.AUDIO
    if requested is SelectionMode.VIDEO and any(
        platform.value in _SOCIAL_MEDIA_PLATFORMS for platform in platforms
    ):
        return SelectionMode.MEDIA
    return requested
