from __future__ import annotations

from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from downloader_bot.application.access import (
    CheckAccess,
    GenerateInvite,
    ListInvites,
    RedeemInvite,
    RevokeInvite,
)
from downloader_bot.application.use_cases import (
    BindInlineResult,
    CancelDownload,
    CleanupArtifacts,
    ConfirmSelection,
    CreateSelection,
    CustomizeJob,
    DeliverResult,
    GetStats,
    GetUserJobs,
    ManageSettings,
    PlanSubmission,
    ProcessDownload,
    PublishOutbox,
    RefreshParent,
    RetryInFormat,
    SubmitBatch,
    SubmitDownload,
    UpdateSelection,
)
from downloader_bot.domain import Platform
from downloader_bot.infrastructure.artifacts import FileArtifactStore
from downloader_bot.infrastructure.database import (
    SqlAccessRepository,
    SqlAnalyticsRepository,
    SqlJobRepository,
    SqlMediaCacheRepository,
    SqlSelectionRepository,
    SqlSettingsRepository,
    SqlUserRepository,
    create_engine,
)
from downloader_bot.infrastructure.download import HttpDownloadEngine
from downloader_bot.infrastructure.platforms import DefaultPlatformRegistry
from downloader_bot.infrastructure.redis_streams import RedisJobQueue, RedisProgressBus

from .settings import Settings
from .system import SystemClock, UuidGenerator


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: AsyncEngine
    redis: Redis
    http: httpx.AsyncClient
    jobs: SqlJobRepository
    users: SqlUserRepository
    preferences: SqlSettingsRepository
    analytics: SqlAnalyticsRepository
    cache: SqlMediaCacheRepository
    selections: SqlSelectionRepository
    access: SqlAccessRepository
    artifacts: FileArtifactStore
    queue: RedisJobQueue
    progress: RedisProgressBus
    submit: SubmitDownload
    submit_batch: SubmitBatch
    create_selection: CreateSelection
    update_selection: UpdateSelection
    confirm_selection: ConfirmSelection
    get_user_jobs: GetUserJobs
    retry_format: RetryInFormat
    cancel: CancelDownload
    bind_inline: BindInlineResult
    customize_job: CustomizeJob
    refresh_parent: RefreshParent
    cleanup_artifacts: CleanupArtifacts
    process: ProcessDownload
    manage_settings: ManageSettings
    plan_submission: PlanSubmission
    publish_outbox: PublishOutbox
    get_stats: GetStats
    check_access: CheckAccess
    redeem_invite: RedeemInvite
    generate_invite: GenerateInvite
    list_invites: ListInvites
    revoke_invite: RevokeInvite

    async def close(self) -> None:
        await self.http.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


async def build_container(settings: Settings) -> Container:
    engine, sessions = create_engine(settings.database_url)
    redis = Redis.from_url(
        settings.redis_url,
        socket_timeout=None,
        socket_connect_timeout=5,
    )
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(60, read=600), limits=httpx.Limits(max_connections=100)
    )
    jobs = SqlJobRepository(sessions)
    users = SqlUserRepository(sessions)
    preferences = SqlSettingsRepository(sessions)
    analytics = SqlAnalyticsRepository(sessions)
    cache = SqlMediaCacheRepository(sessions)
    selections = SqlSelectionRepository(sessions)
    access = SqlAccessRepository(sessions)
    artifacts = FileArtifactStore(settings.artifact_root)
    queue = RedisJobQueue(redis)
    progress = RedisProgressBus(redis)
    clock = SystemClock()
    await queue.initialize()
    await progress.initialize()
    ids = UuidGenerator()
    registry = DefaultPlatformRegistry(
        cookies_file=settings.cookies_file,
        cookies_files={
            Platform.YOUTUBE: settings.youtube_cookies_file,
            Platform.TIKTOK: settings.tiktok_cookies_file,
            Platform.INSTAGRAM: settings.instagram_cookies_file,
            Platform.X: settings.x_cookies_file,
        },
        youtube_pot_provider_url=settings.youtube_pot_provider_url,
        client=http,
        spotify_client_id=settings.spotify_client_id,
        spotify_client_secret=settings.spotify_client_secret,
        spotify_market=settings.spotify_market,
        spotify_command=settings.spotify_command,
        spotify_cache_dir=str(settings.spotify_cache_dir),
        spotify_resolve_timeout_seconds=settings.spotify_resolve_timeout_seconds,
    )
    submit = SubmitDownload(jobs, users, ids, clock, analytics, preferences)
    submit_batch = SubmitBatch(submit, jobs, ids, clock, preferences)
    return Container(
        settings=settings,
        engine=engine,
        redis=redis,
        http=http,
        jobs=jobs,
        users=users,
        preferences=preferences,
        analytics=analytics,
        cache=cache,
        selections=selections,
        access=access,
        artifacts=artifacts,
        queue=queue,
        progress=progress,
        submit=submit,
        submit_batch=submit_batch,
        create_selection=CreateSelection(
            selections, users, preferences, registry, ids, clock, analytics
        ),
        update_selection=UpdateSelection(selections, clock, analytics),
        confirm_selection=ConfirmSelection(
            selections, submit, submit_batch, clock, analytics
        ),
        get_user_jobs=GetUserJobs(jobs),
        retry_format=RetryInFormat(jobs, submit, analytics),
        cancel=CancelDownload(jobs, analytics),
        bind_inline=BindInlineResult(jobs),
        customize_job=CustomizeJob(jobs),
        refresh_parent=RefreshParent(jobs, progress),
        cleanup_artifacts=CleanupArtifacts(jobs, artifacts, clock),
        process=ProcessDownload(
            jobs,
            registry,
            HttpDownloadEngine(
                http,
                settings.artifact_root,
                max_file_size=settings.max_file_size,
                max_parallel_downloads=settings.max_parallel_downloads,
                spotify_command=settings.spotify_command or "spotify-streamer",
                spotify_cache_dir=str(settings.spotify_cache_dir),
                spotify_bitrate=settings.spotify_bitrate,
                youtube_pot_provider_url=settings.youtube_pot_provider_url,
            ),
            artifacts,
            progress,
            analytics,
            clock,
            cache,
        ),
        manage_settings=ManageSettings(preferences),
        plan_submission=PlanSubmission(preferences, registry),
        publish_outbox=PublishOutbox(jobs, queue),
        get_stats=GetStats(analytics),
        check_access=CheckAccess(access),
        redeem_invite=RedeemInvite(access, clock),
        generate_invite=GenerateInvite(access, clock),
        list_invites=ListInvites(access, clock),
        revoke_invite=RevokeInvite(access, clock),
    )


def build_deliver(container: Container, telegram) -> DeliverResult:
    return DeliverResult(
        container.jobs,
        container.artifacts,
        telegram,
        container.analytics,
        container.progress,
    )
