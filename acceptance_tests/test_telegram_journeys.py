from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

from downloader_bot.adapters.telegram.router import build_router
from downloader_bot.domain import (
    DeliveryMode,
    Job,
    JobKind,
    JobStage,
    Platform,
    SelectionMode,
    SelectionRequest,
    UserPreferences,
)


class Submit:
    def __init__(self) -> None:
        self.commands = []

    async def execute(self, command):
        self.commands.append(command)
        return Job(
            "single",
            command.user_id,
            command.chat_id,
            command.url,
            "key",
            kind=command.kind,
            source_message_id=command.source_message_id,
            business_connection_id=command.business_connection_id,
            audio_only=command.audio_only,
        ), True


class Batch:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, **values):
        self.calls.append(values)
        return (
            Job(
                "parent",
                values["user_id"],
                values["chat_id"],
                "batch://links",
                "batch",
                kind=JobKind.BATCH,
                is_parent=True,
                children_total=len(values["urls"]),
            ),
            (),
        )


class Gateway:
    def __init__(self) -> None:
        self.status = []
        self.deleted = []
        self.waiting = []

    async def show_waiting(self, chat_id, business_connection_id=None):
        self.waiting.append((chat_id, business_connection_id))
        return 88

    async def show_status(self, job, progress):
        self.status.append((job, progress))
        return 99

    async def delete_source(self, chat_id, message_id, business_connection_id=None):
        self.deleted.append((chat_id, message_id, business_connection_id))


class SelectionCreator:
    def __init__(self) -> None:
        self.calls = []
        self.bound = []

    async def execute(self, **values):
        self.calls.append(values)
        now = datetime.now(UTC)
        return SelectionRequest(
            token="selection-token",
            user_id=values["user_id"],
            chat_id=values["chat_id"],
            urls=values["urls"],
            platforms=(Platform.YOUTUBE,),
            mode=SelectionMode.VIDEO,
            quality="best",
            delivery=DeliveryMode.MEDIA,
            chapter_count=values.get("chapter_count", 0),
            job_kind=values.get("kind", JobKind.DIRECT),
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )

    async def bind_message(self, *values):
        self.bound.append(values)


class SelectionGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.selections = []
        self.updated = []

    async def show_selection(self, selection):
        self.selections.append(selection)
        return 77

    async def update_selection(self, selection):
        self.updated.append(selection)
        self.selections.append(selection)


class SelectionActions:
    def __init__(self, selection) -> None:
        self.selection = selection
        self.expired = []

    async def get(self, _token):
        return self.selection

    async def execute(self, _token, _user_id, *, action, value):
        if action == "quality":
            self.selection = replace(self.selection, quality=value)
        elif action == "mode":
            self.selection = replace(self.selection, mode=SelectionMode(value))
        return self.selection

    async def cancel(self, _token, _user_id):
        self.selection = replace(self.selection, cancelled_at=datetime.now(UTC))
        return self.selection

    async def record_expired(self, user_id):
        self.expired.append(user_id)


class Confirm:
    def __init__(self, selection) -> None:
        self.selection = selection
        self.calls = []

    async def execute(self, _token, _user_id, **kwargs):
        self.calls.append((_token, _user_id, kwargs))
        return (
            self.selection,
            Job(
                "confirmed",
                self.selection.user_id,
                self.selection.chat_id,
                self.selection.urls[0],
                "key",
                status_message_id=77,
            ),
            True,
        )


class Jobs:
    async def transition(self, job_id, expected, stage, **changes):
        return None

    async def queue_position(self, _job_id):
        return 3


class Stub:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, *_args, **_kwargs):
        self.calls.append((_args, _kwargs))

    async def get(self, _user_id):
        return UserPreferences()

    async def update(self, _user_id, **changes):
        return replace(UserPreferences(), **changes)


class SubmissionPlanner:
    def __init__(
        self,
        *,
        audio_only_by_url=(False,),
        ask_for_youtube=False,
        chapter_count=0,
    ) -> None:
        self.audio_only_by_url = audio_only_by_url
        self.ask_for_youtube = ask_for_youtube
        self.chapter_count = chapter_count

    async def execute(self, _user_id, urls):
        modes = self.audio_only_by_url
        if len(modes) != len(urls):
            modes = tuple(index % 2 == 0 for index, _url in enumerate(urls))
        return SimpleNamespace(
            audio_only_by_url=modes,
            ask_for_youtube=self.ask_for_youtube,
            chapter_count=self.chapter_count,
        )


def handler(router, observer: str, name: str):
    return next(
        item.callback
        for item in router.observers[observer].handlers
        if item.callback.__name__ == name
    )


def make_router():
    submit, batch, gateway = Submit(), Batch(), Gateway()
    stub = Stub()
    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
    )
    return router, submit, batch, gateway


@pytest.mark.asyncio
async def test_link_is_acknowledged_before_provider_preflight() -> None:
    submit, batch, gateway = Submit(), Batch(), Gateway()
    stub = Stub()

    class Planner:
        async def execute(self, _user_id, _urls):
            assert gateway.waiting == [(2, None)]
            return SimpleNamespace(
                audio_only_by_url=(False,),
                ask_for_youtube=False,
                chapter_count=0,
            )

    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        plan_submission=Planner(),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=2, type=ChatType.PRIVATE),
        text="https://youtube.com/watch?v=x",
        message_id=3,
        business_connection_id=None,
    )

    await handler(router, "message", "links")(message)

    assert submit.commands[-1].status_message_id == 88


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "business_id", "kind"),
    [
        (ChatType.PRIVATE, None, JobKind.DIRECT),
        (ChatType.GROUP, None, JobKind.GROUP_REPLAY),
        (ChatType.PRIVATE, "business-1", JobKind.BUSINESS),
    ],
)
async def test_direct_business_and_group_use_same_pipeline(
    chat_type, business_id, kind
) -> None:
    router, submit, _batch, gateway = make_router()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=8, type=chat_type),
        text="https://example.com/video",
        message_id=9,
        business_connection_id=business_id,
    )
    await handler(router, "message", "links")(message)
    assert submit.commands[-1].kind is kind
    assert gateway.status[-1][0].kind is kind


@pytest.mark.asyncio
async def test_batch_and_audio_command_are_transport_independent() -> None:
    router, submit, batch, _gateway = make_router()
    links = handler(router, "message", "links")
    base = {
        "from_user": SimpleNamespace(id=1),
        "chat": SimpleNamespace(id=2, type=ChatType.PRIVATE),
        "message_id": 3,
        "business_connection_id": None,
    }
    await links(SimpleNamespace(**base, text="!audio https://example.com/x"))
    assert submit.commands[-1].audio_only
    await links(
        SimpleNamespace(**base, text="!audio https://a.example/x https://b.example/y")
    )
    assert batch.calls[-1]["audio_only"]
    assert len(batch.calls[-1]["urls"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ("!a", "!mp3", "!music", "/audio", "/A@DownloaderBot"),
)
async def test_audio_command_aliases_keep_the_old_force_audio_behavior(command) -> None:
    router, submit, _batch, _gateway = make_router()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=2, type=ChatType.PRIVATE),
        text=f"{command} https://example.com/media.",
        message_id=3,
        business_connection_id=None,
    )
    await handler(router, "message", "links")(message)
    assert submit.commands[-1].audio_only is True
    assert submit.commands[-1].url == "https://example.com/media"


@pytest.mark.asyncio
async def test_plain_youtube_link_starts_video_unless_user_always_asks() -> None:
    submit, batch, gateway = Submit(), Batch(), SelectionGateway()
    create, stub = SelectionCreator(), Stub()
    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        create_selection=create,
        update_selection=stub,
        confirm_selection=stub,
        plan_submission=SubmissionPlanner(),
    )
    base = {
        "from_user": SimpleNamespace(id=1),
        "chat": SimpleNamespace(id=2, type=ChatType.PRIVATE),
        "message_id": 3,
        "business_connection_id": None,
    }
    links = handler(router, "message", "links")
    await links(SimpleNamespace(**base, text="https://youtube.com/watch?v=x"))
    assert submit.commands[-1].audio_only is False
    assert not gateway.selections

    ask_router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        create_selection=create,
        update_selection=stub,
        confirm_selection=stub,
        plan_submission=SubmissionPlanner(ask_for_youtube=True),
    )
    await handler(ask_router, "message", "links")(
        SimpleNamespace(**base, text="https://youtube.com/watch?v=ask")
    )
    assert gateway.selections and create.bound[-1][-1] == 88

    await links(SimpleNamespace(**base, text="/video https://youtube.com/watch?v=x"))
    assert submit.commands[-1].audio_only is False
    assert gateway.status[-1][0].id == "single"


@pytest.mark.asyncio
async def test_timestamped_youtube_video_always_opens_chapter_choice() -> None:
    submit, batch, gateway = Submit(), Batch(), SelectionGateway()
    create, stub = SelectionCreator(), Stub()
    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        create_selection=create,
        update_selection=stub,
        confirm_selection=stub,
        plan_submission=SubmissionPlanner(chapter_count=13),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=2, type=ChatType.PRIVATE),
        text="/video https://youtu.be/y_y5T1a1iq4",
        message_id=3,
        business_connection_id=None,
    )
    await handler(router, "message", "links")(message)
    assert not submit.commands
    assert create.calls[-1]["chapter_count"] == 13
    assert create.calls[-1]["mode_override"] is SelectionMode.VIDEO
    assert gateway.selections[-1].chapter_count == 13


@pytest.mark.asyncio
async def test_explicit_video_still_overrides_saved_audio_mode_after_preflight() -> None:
    submit, batch, gateway = Submit(), Batch(), Gateway()
    stub = Stub()
    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        plan_submission=SubmissionPlanner(audio_only_by_url=(True,)),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=2, type=ChatType.PRIVATE),
        text="/video https://youtube.com/watch?v=x",
        message_id=3,
        business_connection_id=None,
    )
    await handler(router, "message", "links")(message)
    assert submit.commands[-1].audio_only is False


@pytest.mark.asyncio
async def test_fast_command_with_youtube_playlist_still_requires_scope_choice() -> None:
    submit, batch, gateway = Submit(), Batch(), SelectionGateway()
    create, stub = SelectionCreator(), Stub()
    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        create_selection=create,
        update_selection=stub,
        confirm_selection=stub,
        plan_submission=SubmissionPlanner(),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=2, type=ChatType.PRIVATE),
        text="/audio https://youtu.be/video?list=PL123",
        message_id=3,
        business_connection_id=None,
    )
    await handler(router, "message", "links")(message)
    assert not submit.commands
    assert create.calls[-1]["mode_override"] is SelectionMode.AUDIO
    assert gateway.selections


@pytest.mark.asyncio
async def test_plain_audio_first_link_and_mixed_batch_use_natural_modes() -> None:
    submit, batch, gateway = Submit(), Batch(), Gateway()
    stub = Stub()
    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        plan_submission=SubmissionPlanner(audio_only_by_url=(True,)),
    )
    base = {
        "from_user": SimpleNamespace(id=1),
        "chat": SimpleNamespace(id=2, type=ChatType.PRIVATE),
        "message_id": 3,
        "business_connection_id": None,
    }
    links = handler(router, "message", "links")
    await links(SimpleNamespace(**base, text="https://soundcloud.com/a/b"))
    assert submit.commands[-1].audio_only

    await links(
        SimpleNamespace(
            **base,
            text="https://spotify.example/a https://instagram.example/b",
        )
    )
    assert batch.calls[-1]["audio_only_by_url"] == (True, False)


@pytest.mark.asyncio
async def test_inline_queues_same_job_and_returns_personal_placeholder() -> None:
    router, submit, _batch, _gateway = make_router()
    answers = []

    async def answer(*args, **kwargs):
        answers.append((args, kwargs))

    query = SimpleNamespace(
        query="https://example.com/video",
        from_user=SimpleNamespace(id=5),
        answer=answer,
    )
    await handler(router, "inline_query", "inline")(query)
    assert submit.commands[-1].kind is JobKind.INLINE
    assert answers[-1][1]["is_personal"] is True
    assert answers[-1][0][0][0].id == "job:single"


@pytest.mark.asyncio
async def test_inline_always_ask_routes_to_private_without_creating_job() -> None:
    submit, batch, gateway = Submit(), Batch(), Gateway()
    stub = Stub()
    create = SelectionCreator()
    router = build_router(
        submit,
        batch,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        gateway,
        Jobs(),
        create_selection=create,
        confirm_selection=stub,
        plan_submission=SubmissionPlanner(ask_for_youtube=True),
    )
    answers = []

    async def answer(*args, **kwargs):
        answers.append((args, kwargs))

    query = SimpleNamespace(
        query="https://example.com/video",
        from_user=SimpleNamespace(id=5),
        answer=answer,
    )
    await handler(router, "inline_query", "inline")(query)
    assert answers[-1][0][0] == []
    assert answers[-1][1]["button"].start_parameter == "inline"
    assert not create.calls
    assert not submit.commands


@pytest.mark.asyncio
async def test_inline_playlist_routes_to_private_scope_picker() -> None:
    router, submit, _batch, _gateway = make_router()
    answers = []

    async def answer(*args, **kwargs):
        answers.append((args, kwargs))

    query = SimpleNamespace(
        query="https://youtu.be/video?list=PL123",
        from_user=SimpleNamespace(id=5),
        answer=answer,
    )
    await handler(router, "inline_query", "inline")(query)
    assert not submit.commands
    assert answers[-1][0][0] == []
    assert answers[-1][1]["button"].start_parameter == "inline"


@pytest.mark.asyncio
async def test_commands_settings_and_job_callbacks_are_mocked() -> None:
    submit, batch, gateway = Submit(), Batch(), Gateway()
    cancel, bind, customize, deliver, settings, stats = (Stub() for _ in range(6))
    stats.execute = lambda: _value({"job_delivered": 4, "job_failed": 1})
    router = build_router(
        submit,
        batch,
        cancel,
        bind,
        customize,
        deliver,
        settings,
        stats,
        gateway,
        Jobs(),
        frozenset({1}),
    )
    answers = []

    async def answer(text, **kwargs):
        answers.append((text, kwargs))

    message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=answer)
    await handler(router, "message", "start")(message)
    await handler(router, "message", "show_settings")(message)
    await handler(router, "message", "show_stats")(message)
    await handler(router, "message", "show_admin")(message)
    assert len(answers) == 4

    query_answers = []

    async def query_answer(*args, **kwargs):
        query_answers.append((args, kwargs))

    async def edit_text(*_args, **_kwargs):
        return None

    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="settings:toggle:show_buttons",
        message=SimpleNamespace(edit_text=edit_text),
        answer=query_answer,
    )
    await handler(router, "callback_query", "update_settings")(query)
    query.data = "job:cancel:abc"
    await handler(router, "callback_query", "cancel_job")(query)
    query.data = "job:deliver:abc"
    await handler(router, "callback_query", "retry_delivery")(query)
    assert query_answers


@pytest.mark.asyncio
async def test_navigation_selection_status_and_result_callbacks(monkeypatch) -> None:
    monkeypatch.setattr(
        "downloader_bot.adapters.telegram.router.Message", SimpleNamespace
    )
    now = datetime.now(UTC)
    selection = SelectionRequest(
        token="selection-token",
        user_id=1,
        chat_id=2,
        urls=("https://youtube.com/watch?v=x",),
        platforms=(Platform.YOUTUBE,),
        mode=SelectionMode.VIDEO,
        quality="best",
        delivery=DeliveryMode.MEDIA,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        status_message_id=77,
        chapter_count=13,
    )
    selection_actions = SelectionActions(selection)
    confirm = Confirm(selection)
    submit, batch, gateway, create = (
        Submit(),
        Batch(),
        SelectionGateway(),
        SelectionCreator(),
    )
    settings, stats, cancel, bind, customize, deliver = (Stub() for _ in range(6))
    stats.execute = lambda: _value({"job_delivered": 2, "job_failed": 1})
    source = Job(
        "source",
        1,
        2,
        "https://youtube.com/watch?v=x",
        "source-key",
        stage=JobStage.FAILED,
    )

    class FullJobs(Jobs):
        async def get(self, job_id):
            return source if job_id == "source" else None

    class Recent:
        async def execute(self, _user_id):
            return (source,)

    class Retry:
        async def execute(self, _job_id, _user_id, _action):
            return Job("retry", 1, 2, source.source_url, "retry-key")

    analytics = SimpleNamespace(events=[])

    async def record(event, **_values):
        analytics.events.append(event)

    analytics.record = record
    router = build_router(
        submit,
        batch,
        cancel,
        bind,
        customize,
        deliver,
        settings,
        stats,
        gateway,
        FullJobs(),
        frozenset({1}),
        create_selection=create,
        update_selection=selection_actions,
        confirm_selection=confirm,
        get_user_jobs=Recent(),
        retry_format=Retry(),
        ux_analytics=analytics,
    )
    answers, edits, callback_answers = [], [], []

    async def answer(*args, **kwargs):
        answers.append((args, kwargs))
        return SimpleNamespace(message_id=88)

    async def edit_text(*args, **kwargs):
        edits.append((args, kwargs))

    async def callback_answer(*args, **kwargs):
        callback_answers.append((args, kwargs))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), answer=answer, edit_text=edit_text
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="nav:help",
        message=message,
        answer=callback_answer,
    )
    await handler(router, "message", "show_help")(message)
    await handler(router, "callback_query", "callback_help")(query)
    query.data = "nav:settings"
    await handler(router, "callback_query", "callback_settings")(query)
    for data in (
        "settings:page:download",
        "settings:youtube:ask",
        "settings:set:document_mode:true",
        "settings:toggle:compact_progress",
        "settings:home",
    ):
        query.data = data
        await handler(router, "callback_query", "update_settings")(query)
    await handler(router, "message", "show_status")(message)

    query.data = "sel:start:split:selection-token"
    await handler(router, "callback_query", "selection_action")(query)
    assert gateway.status[-1][0].id == "confirmed"
    assert selection_actions.selection.mode is SelectionMode.SPLIT
    selection_actions.selection = replace(selection, cancelled_at=None)
    query.data = "sel:cancel:selection-token"
    await handler(router, "callback_query", "selection_action")(query)
    assert edits

    query.data = "result:retry:source"
    await handler(router, "callback_query", "result_action")(query)
    assert gateway.status[-1][0].id == "retry"
    query.data = "result:format:source"
    await handler(router, "callback_query", "result_action")(query)
    assert gateway.selections
    assert {"help_opened", "settings_opened"} <= set(analytics.events)
    assert callback_answers and answers


async def _value(value):
    return value
