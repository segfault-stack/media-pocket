from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from downloader_bot.application.progress import ProgressThrottle
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
    ProcessInline,
    PublishOutbox,
    RefreshParent,
    RepositoryCancellation,
    RetryInFormat,
    SubmitBatch,
    SubmitDownload,
    SubmitDownloadCommand,
    UpdateSelection,
)
from downloader_bot.domain import (
    DeliveryMode,
    DownloadArtifact,
    ErrorCode,
    Job,
    JobKind,
    JobStage,
    MediaAsset,
    MediaKind,
    MediaPost,
    Platform,
    Progress,
    SelectionMode,
    SelectionRequest,
    UserPreferences,
)
from downloader_bot.domain.errors import DownloadError
from downloader_bot.domain.models import InvalidJobTransition


class Jobs:
    def __init__(self) -> None:
        self.items: dict[str, Job] = {}
        self.dedupe: dict[str, str] = {}
        self.outbox = [(1, "job")]
        self.published = []
        self.cleaned = []

    async def create_with_outbox(self, job: Job):
        if job.dedupe_key in self.dedupe:
            return self.items[self.dedupe[job.dedupe_key]], False
        self.items[job.id] = job
        self.dedupe[job.dedupe_key] = job.id
        return job, True

    async def create_parent(self, job: Job):
        self.items[job.id] = job
        return job

    async def create_batch(self, parent: Job, children: tuple[Job, ...]):
        self.items[parent.id] = parent
        for child in children:
            self.items[child.id] = child
            self.dedupe[child.dedupe_key] = child.id

    async def get(self, job_id: str):
        return self.items.get(job_id)

    async def children(self, parent_id: str):
        return tuple(
            item for item in self.items.values() if item.parent_id == parent_id
        )

    async def transition(self, job_id, expected, stage, **changes):
        job = self.items[job_id]
        if job.stage not in expected:
            return None
        job = replace(job, stage=stage, updated_at=datetime.now(UTC), **changes)
        self.items[job_id] = job
        return job

    async def request_cancel(self, job_id, user_id):
        job = self.items.get(job_id)
        if not job or job.user_id != user_id or job.terminal:
            return None
        job = replace(job, cancel_requested=True)
        self.items[job_id] = job
        return job

    async def request_cancel_children(self, parent_id, user_id):
        count = 0
        for child in await self.children(parent_id):
            if child.user_id == user_id and not child.terminal:
                self.items[child.id] = replace(child, cancel_requested=True)
                count += 1
        return count

    async def bind_inline(self, job_id, user_id, inline_message_id):
        job = self.items.get(job_id)
        if not job or job.user_id != user_id or job.kind is not JobKind.INLINE:
            return None
        job = replace(job, inline_message_id=inline_message_id)
        self.items[job_id] = job
        return job

    async def customize(self, job_id, user_id, *, audio_only=None, document_mode=None):
        job = self.items.get(job_id)
        if not job or job.user_id != user_id or job.stage is not JobStage.QUEUED:
            return None
        preferences = job.preferences
        if document_mode is not None:
            preferences = replace(preferences, document_mode=document_mode)
        job = replace(
            job,
            audio_only=job.audio_only if audio_only is None else audio_only,
            preferences=preferences,
        )
        self.items[job_id] = job
        return job

    async def claim_outbox(self, limit=100):
        return tuple(self.outbox[:limit])

    async def mark_outbox_published(self, event_id):
        self.published.append(event_id)

    async def expired_artifacts(self, _before, limit=100):
        return tuple(self.cleaned[:limit])

    async def mark_artifacts_cleaned(self, job_id, at):
        self.items[job_id] = replace(self.items[job_id], artifacts_cleaned_at=at)

    async def queue_position(self, _job_id):
        return 1

    async def recent_for_user(self, user_id, *, limit=10):
        values = [item for item in self.items.values() if item.user_id == user_id]
        return tuple(values[::-1][:limit])


class Settings:
    def __init__(self, value=None) -> None:
        self.value = value or UserPreferences()

    async def get(self, _user_id):
        return self.value

    async def save(self, _user_id, value):
        self.value = value
        return value


class Selections:
    def __init__(self) -> None:
        self.items: dict[str, SelectionRequest] = {}

    async def create(self, selection):
        self.items[selection.token] = selection
        return selection

    async def get(self, token):
        return self.items.get(token)

    async def update(self, token, user_id, now, **changes):
        selection = self.items.get(token)
        if (
            selection is None
            or selection.user_id != user_id
            or not selection.active
            or selection.expires_at <= now
        ):
            return None
        selection = replace(selection, **changes)
        self.items[token] = selection
        return selection

    async def claim(self, token, user_id, now):
        return await self.update(token, user_id, now, claimed_at=now)

    async def release_claim(self, token, user_id, claimed_at):
        selection = self.items.get(token)
        if (
            selection is not None
            and selection.user_id == user_id
            and selection.claimed_at == claimed_at
        ):
            self.items[token] = replace(selection, claimed_at=None)

    async def cancel(self, token, user_id, now):
        return await self.update(token, user_id, now, cancelled_at=now)


class Users:
    async def ensure(self, _user_id):
        return None


class Ids:
    def __init__(self) -> None:
        self.index = 0

    def new(self):
        self.index += 1
        return f"job-{self.index}"


class Clock:
    def __init__(self) -> None:
        self.tick = 0.0

    def now(self):
        return datetime(2026, 8, 21, tzinfo=UTC)

    def monotonic(self):
        return self.tick


class Analytics:
    def __init__(self) -> None:
        self.events = []

    async def record(self, event, **_values):
        self.events.append(event)

    async def stats(self):
        return {"events": len(self.events)}


class Bus:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, value):
        self.events.append(value)


ARTIFACT = DownloadArtifact(PurePosixPath("/tmp/media.mp4"), MediaKind.VIDEO, 4, "sum")


class Artifacts:
    def __init__(self) -> None:
        self.value = ()
        self.cleaned = []

    async def persist(self, _job_id, artifacts):
        self.value = artifacts

    async def get(self, _job_id):
        return self.value

    async def cleanup(self, job_id):
        self.cleaned.append(job_id)


class Cache:
    def __init__(self, value=None) -> None:
        self.value = value
        self.keys = []
        self.puts = []

    async def get(self, key):
        self.keys.append(key)
        return self.value

    async def put(self, key, value):
        self.keys.append(key)
        self.puts.append((key, value))
        self.value = value


class Adapter:
    platform = Platform.GENERIC

    async def resolve(self, url, preferences, *, audio_only=False):
        assert preferences.quality
        return MediaPost(
            url, self.platform, (MediaAsset("https://cdn/x.mp4", MediaKind.VIDEO),)
        )


class Registry:
    def detect(self, _url):
        return Adapter()


class YoutubeRegistry:
    def detect(self, _url):
        return SimpleAdapter(Platform.YOUTUBE)


class SimpleAdapter:
    def __init__(self, platform) -> None:
        self.platform = platform


class Engine:
    async def download(self, _post, job, progress, _cancellation):
        await progress(Progress(job.id, JobStage.DOWNLOADING, 50))
        return (ARTIFACT,)


class Queue:
    def __init__(self) -> None:
        self.items = []

    async def publish(self, job_id):
        self.items.append(job_id)
        return "1-0"


@pytest.mark.parametrize("target", [JobStage.DELIVERED, JobStage.PROCESSING])
def test_state_machine_rejects_skipped_stages(target) -> None:
    job = Job("job", 1, 1, "https://example.com", "key")
    with pytest.raises(InvalidJobTransition):
        job.transition(target)


def test_progress_throttle_contract() -> None:
    clock = Clock()
    throttle = ProgressThrottle(clock)
    assert throttle.accept(Progress("job", JobStage.DOWNLOADING, 10))
    assert not throttle.accept(Progress("job", JobStage.DOWNLOADING, 11))
    assert throttle.accept(Progress("job", JobStage.DOWNLOADING, 12))
    clock.tick = 2.1
    assert throttle.accept(Progress("job", JobStage.DOWNLOADING, 13))


@pytest.mark.asyncio
async def test_submit_dedupes_same_variant_but_not_audio() -> None:
    jobs, ids, analytics = Jobs(), Ids(), Analytics()
    submit = SubmitDownload(jobs, Users(), ids, Clock(), analytics, Settings())
    first, created = await submit.execute(
        SubmitDownloadCommand(1, 1, "https://example.com/x")
    )
    duplicate, duplicate_created = await submit.execute(
        SubmitDownloadCommand(1, 2, "https://example.com/x")
    )
    audio, audio_created = await submit.execute(
        SubmitDownloadCommand(1, 1, "https://example.com/x", audio_only=True)
    )
    assert created and not duplicate_created and audio_created
    assert first.id == duplicate.id and audio.id != first.id


@pytest.mark.asyncio
async def test_batch_creates_parent_and_children_with_snapshot() -> None:
    jobs, ids, settings = Jobs(), Ids(), Settings(UserPreferences(quality="720"))
    submit = SubmitDownload(jobs, Users(), ids, Clock(), Analytics(), settings)
    parent, children = await SubmitBatch(submit, jobs, ids, Clock(), settings).execute(
        user_id=1,
        chat_id=2,
        urls=("https://a.example/x", "https://b.example/y"),
        audio_only=True,
    )
    assert parent.is_parent and parent.kind is JobKind.BATCH
    assert parent.children_total == 2
    assert all(child.parent_id == parent.id and child.audio_only for child in children)
    assert all(child.preferences.quality == "720" for child in children)


@pytest.mark.asyncio
async def test_natural_submission_plan_and_per_url_batch_modes() -> None:
    class NaturalRegistry:
        def detect(self, url):
            if "spotify" in url:
                return SimpleAdapter(Platform.SPOTIFY)
            if "instagram" in url:
                return SimpleAdapter(Platform.INSTAGRAM)
            return SimpleAdapter(Platform.YOUTUBE)

    urls = (
        "https://open.spotify.com/track/x",
        "https://instagram.com/p/x",
        "https://youtube.com/watch?v=x",
    )
    settings = Settings(UserPreferences(youtube_mode="audio"))
    plan = await PlanSubmission(settings, NaturalRegistry()).execute(1, urls)
    assert plan.audio_only_by_url == (True, False, True)
    assert not plan.ask_for_youtube

    jobs, ids = Jobs(), Ids()
    submit = SubmitDownload(jobs, Users(), ids, Clock(), Analytics(), settings)
    _, children = await SubmitBatch(submit, jobs, ids, Clock(), settings).execute(
        user_id=1,
        chat_id=2,
        urls=urls,
        audio_only_by_url=plan.audio_only_by_url,
    )
    assert tuple(child.audio_only for child in children) == (True, False, True)
    with pytest.raises(ValueError, match="batch modes must match URLs"):
        await SubmitBatch(submit, jobs, ids, Clock(), settings).execute(
            user_id=1,
            chat_id=2,
            urls=urls,
            audio_only_by_url=(True,),
        )


@pytest.mark.asyncio
async def test_youtube_ask_plan_only_asks_when_youtube_is_present() -> None:
    class NaturalRegistry:
        def detect(self, url):
            platform = Platform.YOUTUBE if "youtube" in url else Platform.SOUNDCLOUD
            return SimpleAdapter(platform)

    planner = PlanSubmission(
        Settings(UserPreferences(youtube_mode="ask")), NaturalRegistry()
    )
    youtube = await planner.execute(1, ("https://youtube.com/watch?v=x",))
    soundcloud = await planner.execute(1, ("https://soundcloud.com/a/b",))
    assert youtube.ask_for_youtube and youtube.audio_only_by_url == (False,)
    assert not soundcloud.ask_for_youtube
    assert soundcloud.audio_only_by_url == (True,)


@pytest.mark.asyncio
async def test_batch_keeps_duplicate_urls_scoped_to_the_same_parent() -> None:
    jobs, ids, settings = Jobs(), Ids(), Settings()
    submit = SubmitDownload(jobs, Users(), ids, Clock(), Analytics(), settings)
    parent, children = await SubmitBatch(submit, jobs, ids, Clock(), settings).execute(
        user_id=1,
        chat_id=2,
        urls=("https://example.com/same", "https://example.com/same"),
    )
    assert len(children) == 2
    assert children[0].dedupe_key != children[1].dedupe_key
    assert all(child.parent_id == parent.id for child in children)


@pytest.mark.asyncio
async def test_selection_is_persisted_updated_and_confirmed_once() -> None:
    jobs, ids, analytics = Jobs(), Ids(), Analytics()
    selections = Selections()
    settings = Settings(UserPreferences(quality="1080", document_mode=True))
    submit = SubmitDownload(jobs, Users(), ids, Clock(), analytics, settings)
    batch = SubmitBatch(submit, jobs, ids, Clock(), settings)
    create = CreateSelection(
        selections,
        Users(),
        settings,
        YoutubeRegistry(),
        ids,
        Clock(),
        analytics,
    )
    selection = await create.execute(
        user_id=7,
        chat_id=8,
        urls=("https://youtube.com/watch?v=x",),
        source_message_id=9,
    )
    assert selection.mode is SelectionMode.VIDEO
    assert selection.quality == "1080"
    assert selection.delivery is DeliveryMode.FILE
    assert selection.expires_at > selection.created_at

    update = UpdateSelection(selections, Clock(), analytics)
    selection = await update.execute(selection.token, 7, action="mode", value="audio")
    assert selection and selection.mode is SelectionMode.AUDIO

    confirm = ConfirmSelection(selections, submit, batch, Clock(), analytics)
    _, job, claimed = await confirm.execute(selection.token, 7)
    assert claimed and job and job.audio_only
    assert job.preferences.quality == "1080"
    assert job.preferences.document_mode
    _, duplicate, claimed_again = await confirm.execute(selection.token, 7)
    assert duplicate is None and not claimed_again
    assert analytics.events.count("download_confirmed") == 1


@pytest.mark.asyncio
async def test_failed_confirmation_releases_selection_claim() -> None:
    selections, analytics = Selections(), Analytics()
    selection = await CreateSelection(
        selections,
        Users(),
        Settings(),
        YoutubeRegistry(),
        Ids(),
        Clock(),
        analytics,
    ).execute(user_id=1, chat_id=1, urls=("https://youtube.com/watch?v=x",))

    class FailingSubmit:
        async def execute(self, _command):
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await ConfirmSelection(
            selections,
            FailingSubmit(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            Clock(),
            analytics,
        ).execute(selection.token, 1)
    assert selections.items[selection.token].claimed_at is None


@pytest.mark.asyncio
async def test_selection_rejects_foreign_owner_and_expired_request() -> None:
    selections, analytics = Selections(), Analytics()
    create = CreateSelection(
        selections,
        Users(),
        Settings(),
        YoutubeRegistry(),
        Ids(),
        Clock(),
        analytics,
    )
    selection = await create.execute(
        user_id=1, chat_id=1, urls=("https://youtube.com/watch?v=x",)
    )
    update = UpdateSelection(selections, Clock(), analytics)
    assert (
        await update.execute(selection.token, 2, action="quality", value="720") is None
    )
    selections.items[selection.token] = replace(
        selection, expires_at=selection.created_at
    )
    assert (
        await update.execute(selection.token, 1, action="quality", value="720") is None
    )


@pytest.mark.asyncio
async def test_batch_selection_status_and_result_actions() -> None:
    jobs, ids, analytics, selections = Jobs(), Ids(), Analytics(), Selections()
    settings = Settings(UserPreferences(default_audio_only=True))

    class SocialRegistry:
        def detect(self, url):
            return SimpleAdapter(
                Platform.TIKTOK if "tiktok" in url else Platform.SPOTIFY
            )

    submit = SubmitDownload(jobs, Users(), ids, Clock(), analytics, settings)
    batch = SubmitBatch(submit, jobs, ids, Clock(), settings)
    create = CreateSelection(
        selections, Users(), settings, SocialRegistry(), ids, Clock(), analytics
    )
    selection = await create.execute(
        user_id=1,
        chat_id=2,
        urls=(
            "https://tiktok.com/video/1",
            "https://open.spotify.com/track/2",
        ),
        kind=JobKind.GROUP_REPLAY,
    )
    assert selection.mode is SelectionMode.AUDIO
    assert await create.bind_message(selection.token, 1, 44)
    update = UpdateSelection(selections, Clock(), analytics)
    assert await update.get(selection.token)
    await update.record_expired(1)
    changed = await update.execute(selection.token, 1, action="delivery", value="file")
    assert changed and changed.delivery is DeliveryMode.FILE
    changed = await update.execute(selection.token, 1, action="mode", value="video")
    assert changed and changed.mode is SelectionMode.MEDIA
    with pytest.raises(ValueError, match="invalid selection"):
        await update.execute(selection.token, 1, action="bogus", value="x")

    _, parent, claimed = await ConfirmSelection(
        selections, submit, batch, Clock(), analytics
    ).execute(selection.token, 1)
    assert claimed and parent and parent.is_parent
    assert parent.preferences.document_mode
    recent = await GetUserJobs(jobs).execute(1, limit=2)
    assert len(recent) == 2

    source = replace(recent[0], stage=JobStage.DELIVERED)
    jobs.items[source.id] = source
    retry = RetryInFormat(jobs, submit, analytics)
    audio = await retry.execute(source.id, 1, "audio")
    assert audio and audio.audio_only
    video = await retry.execute(source.id, 1, "video")
    assert video and not video.audio_only
    document = await retry.execute(source.id, 1, "file")
    assert document and document.preferences.document_mode
    assert await retry.execute(source.id, 2, "retry") is None
    assert await retry.execute(source.id, 1, "unknown") is None


@pytest.mark.asyncio
async def test_audio_selection_forces_audio_and_can_be_cancelled() -> None:
    selections, analytics = Selections(), Analytics()

    class SpotifyRegistry:
        def detect(self, _url):
            return SimpleAdapter(Platform.SPOTIFY)

    create = CreateSelection(
        selections,
        Users(),
        Settings(),
        SpotifyRegistry(),
        Ids(),
        Clock(),
        analytics,
    )
    selection = await create.execute(
        user_id=1, chat_id=1, urls=("https://open.spotify.com/track/x",)
    )
    assert selection.mode is SelectionMode.AUDIO
    update = UpdateSelection(selections, Clock(), analytics)
    forced = await update.execute(selection.token, 1, action="mode", value="video")
    assert forced and forced.mode is SelectionMode.AUDIO
    assert await update.cancel(selection.token, 1)


@pytest.mark.asyncio
async def test_pipeline_and_cache_reach_ready() -> None:
    jobs, artifacts, progress, analytics = Jobs(), Artifacts(), Bus(), Analytics()
    job = Job("job", 1, 1, "https://example.com", "key")
    jobs.items[job.id] = job
    result = await ProcessDownload(
        jobs, Registry(), Engine(), artifacts, progress, analytics, Clock(), Cache()
    ).execute(job.id)
    assert result and result.stage is JobStage.READY
    assert artifacts.value == (ARTIFACT,)
    assert "job_ready" in analytics.events


@pytest.mark.asyncio
async def test_pipeline_does_not_cache_when_ready_transition_fails() -> None:
    class ReadyTransitionFails(Jobs):
        async def transition(self, job_id, expected, stage, **changes):
            if stage is JobStage.READY:
                return None
            return await super().transition(job_id, expected, stage, **changes)

    jobs = ReadyTransitionFails()
    artifacts, progress, analytics, cache = Artifacts(), Bus(), Analytics(), Cache()
    job = Job("not-ready", 1, 1, "https://example.com/not-ready", "key")
    jobs.items[job.id] = job

    result = await ProcessDownload(
        jobs, Registry(), Engine(), artifacts, progress, analytics, Clock(), cache
    ).execute(job.id)

    assert result and result.stage is JobStage.PROCESSING
    assert cache.puts == []
    assert "job_ready" not in analytics.events


@pytest.mark.asyncio
async def test_pipeline_cache_hit_skips_resolver_and_cancel_is_terminal() -> None:
    jobs, artifacts, progress, analytics = Jobs(), Artifacts(), Bus(), Analytics()
    cached = Job("cached", 1, 1, "https://example.com", "key")
    jobs.items[cached.id] = cached
    result = await ProcessDownload(
        jobs,
        Registry(),
        Engine(),
        artifacts,
        progress,
        analytics,
        Clock(),
        Cache((ARTIFACT,)),
    ).execute(cached.id)
    assert result and result.stage is JobStage.READY

    cancelled = Job(
        "cancel", 1, 1, "https://example.com/y", "key2", cancel_requested=True
    )
    jobs.items[cancelled.id] = cancelled
    result = await ProcessDownload(
        jobs, Registry(), Engine(), artifacts, progress, analytics, Clock(), Cache()
    ).execute(cancelled.id)
    assert result and result.stage is JobStage.CANCELLED
    assert artifacts.cleaned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retryable", "stage"), [(True, JobStage.RETRYING), (False, JobStage.FAILED)]
)
async def test_pipeline_classifies_provider_failures(retryable, stage) -> None:
    class Broken:
        platform = Platform.GENERIC

        async def resolve(self, *_args, **_kwargs):
            raise DownloadError(
                ErrorCode.RATE_LIMITED if retryable else ErrorCode.PRIVATE,
                "failure",
                retryable=retryable,
            )

    class BrokenRegistry:
        def detect(self, _url):
            return Broken()

    jobs = Jobs()
    job = Job("failure", 1, 1, "https://example.com", "key")
    jobs.items[job.id] = job
    result = await ProcessDownload(
        jobs,
        BrokenRegistry(),
        Engine(),
        Artifacts(),
        Bus(),
        Analytics(),
        Clock(),
        Cache(),
    ).execute(job.id)
    assert result and result.stage is stage


@pytest.mark.asyncio
async def test_cancel_parent_propagates_to_children() -> None:
    jobs = Jobs()
    parent = Job(
        "parent", 1, 1, "batch://links", "p", kind=JobKind.BATCH, is_parent=True
    )
    child = Job("child", 1, 1, "https://example.com", "c", parent_id=parent.id)
    jobs.items = {parent.id: parent, child.id: child}
    result = await CancelDownload(jobs, Analytics()).execute(parent.id, 1)
    assert result and result.stage is JobStage.CANCELLED
    assert jobs.items[child.id].cancel_requested


@pytest.mark.asyncio
async def test_delivery_uses_submitted_preferences_and_manual_retry_guard() -> None:
    jobs, artifacts = Jobs(), Artifacts()
    preferences = UserPreferences(captions=False, document_mode=True)
    job = Job(
        "job",
        1,
        1,
        "https://example.com",
        "key",
        stage=JobStage.READY,
        preferences=preferences,
    )
    jobs.items[job.id] = job
    artifacts.value = (ARTIFACT,)

    class Gateway:
        delivered = None
        manual = False

        async def deliver(self, job, artifacts, preferences):
            self.delivered = preferences

        async def show_manual_retry(self, _job):
            self.manual = True

    gateway = Gateway()
    result = await DeliverResult(jobs, artifacts, gateway, Analytics(), Bus()).execute(
        job.id
    )
    assert result and result.stage is JobStage.DELIVERED
    assert gateway.delivered == preferences
    jobs.items[job.id] = replace(job, stage=JobStage.DELIVERING)
    await DeliverResult(jobs, artifacts, gateway, Analytics(), Bus()).execute(job.id)
    assert gateway.manual


@pytest.mark.asyncio
async def test_inline_customization_settings_stats_and_outbox_use_cases() -> None:
    jobs, settings, analytics = Jobs(), Settings(), Analytics()
    inline = Job("inline", 1, 1, "https://example.com", "key", kind=JobKind.INLINE)
    jobs.items[inline.id] = inline
    assert not await RepositoryCancellation(jobs, inline.id).requested()
    bound = await BindInlineResult(jobs).execute(inline.id, 1, "inline-message")
    assert bound and bound.inline_message_id == "inline-message"
    assert (await CustomizeJob(jobs).audio(inline.id, 1)).audio_only
    assert (await CustomizeJob(jobs).document(inline.id, 1)).preferences.document_mode

    manager = ManageSettings(settings)
    assert (await manager.update(1, quality="480")).quality == "480"
    assert (await manager.get(1)).quality == "480"
    assert (await manager.update(1, youtube_mode="ask")).youtube_mode == "ask"
    with pytest.raises(ValueError, match="invalid YouTube mode"):
        await manager.update(1, youtube_mode="sometimes")
    with pytest.raises(ValueError, match="unknown settings"):
        await manager.update(1, invalid=True)

    analytics.events.extend(["a", "b"])
    assert await GetStats(analytics).execute() == {"events": 2}
    queue = Queue()
    assert await PublishOutbox(jobs, queue).execute() == 1
    assert queue.items == ["job"] and jobs.published == [1]


@pytest.mark.asyncio
async def test_process_inline_parent_refresh_and_retention_cleanup() -> None:
    jobs, ids, settings, progress = Jobs(), Ids(), Settings(), Bus()
    submit = SubmitDownload(jobs, Users(), ids, Clock(), Analytics(), settings)
    inline, created = await ProcessInline(submit).execute(
        user_id=4, url="https://example.com/inline"
    )
    assert created and inline.kind is JobKind.INLINE

    parent = Job(
        "parent",
        1,
        1,
        "batch://links",
        "parent",
        kind=JobKind.BATCH,
        is_parent=True,
        children_total=2,
    )
    child1 = Job(
        "child1",
        1,
        1,
        "https://a.example",
        "a",
        parent_id=parent.id,
        stage=JobStage.DELIVERED,
    )
    child2 = Job(
        "child2",
        1,
        1,
        "https://b.example",
        "b",
        parent_id=parent.id,
        stage=JobStage.FAILED,
    )
    jobs.items.update({item.id: item for item in (parent, child1, child2)})
    refreshed = await RefreshParent(jobs, progress).execute(parent.id)
    assert refreshed and refreshed.stage is JobStage.FAILED
    assert progress.events[-1].item == 2

    artifacts = Artifacts()
    jobs.cleaned = [child1.id]
    assert (
        await CleanupArtifacts(jobs, artifacts, Clock()).execute(retention_seconds=10)
        == 1
    )
    assert artifacts.cleaned == [child1.id]
    assert jobs.items[child1.id].artifacts_cleaned_at is not None


@pytest.mark.asyncio
async def test_invalid_url_is_rejected_before_persistence() -> None:
    with pytest.raises(DownloadError) as error:
        await SubmitDownload(
            Jobs(), Users(), Ids(), Clock(), Analytics(), Settings()
        ).execute(SubmitDownloadCommand(1, 1, "file:///tmp/x"))
    assert error.value.code is ErrorCode.UNSUPPORTED


def test_layers_are_framework_free_and_legacy_free() -> None:
    forbidden_inner = {"aiogram", "sqlalchemy", "redis", "httpx"}
    forbidden_all = {"handlers", "services", "utils", "app_context", "config"}
    for path in Path("downloader_bot").rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        imports = {
            node.names[0].name.split(".")[0]
            if isinstance(node, ast.Import)
            else (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        }
        assert not imports & forbidden_all
        if path.parts[1] in {"domain", "application"}:
            assert not imports & forbidden_inner
