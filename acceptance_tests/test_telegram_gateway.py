from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import UUID

import pytest
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from downloader_bot.adapters.telegram.gateway import (
    AiogramTelegramGateway,
    _can_send_media_groups,
    _chunks,
    _input_media,
    _source_keyboard,
    _status_keyboard,
)
from downloader_bot.domain import (
    DownloadArtifact,
    Job,
    JobStage,
    MediaKind,
    Progress,
    UserPreferences,
)


class Bot:
    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return SimpleNamespace(message_id=77)

        return call


def artifact(path, kind):
    return DownloadArtifact(PurePosixPath(path), kind, 1, "sum")


def assert_uuid_filename(filename: str, suffix: str) -> None:
    assert filename.endswith(suffix)
    UUID(filename.removesuffix(suffix))


@pytest.mark.asyncio
async def test_gateway_creates_and_edits_status_for_direct_business_and_inline() -> (
    None
):
    bot = Bot()
    gateway = AiogramTelegramGateway(bot)
    job = Job(
        "job", 1, 2, "https://example.com", "key", business_connection_id="business"
    )
    assert await gateway.show_status(job, Progress(job.id, JobStage.QUEUED)) == 77
    await gateway.show_status(
        replace(job, status_message_id=77), Progress(job.id, JobStage.DOWNLOADING, 50)
    )
    await gateway.show_status(
        replace(job, inline_message_id="inline"),
        Progress(job.id, JobStage.PROCESSING, 90),
    )
    await gateway.show_status(
        replace(job, status_message_id=77, stage=JobStage.DELIVERED),
        Progress(job.id, JobStage.DELIVERED, 100),
    )
    assert [call[0] for call in bot.calls] == [
        "send_message",
        "edit_message_text",
        "edit_message_text",
        "delete_message",
    ]
    assert bot.calls[0][2]["business_connection_id"] == "business"


@pytest.mark.asyncio
async def test_gateway_delivers_clean_media_then_separate_actions(tmp_path) -> None:
    bot = Bot()
    gateway = AiogramTelegramGateway(bot)
    paths = []
    for suffix in ("jpg", "mp3", "mp4", "bin"):
        path = tmp_path / f"file.{suffix}"
        path.write_bytes(b"x")
        paths.append(path)
    artifacts = tuple(
        artifact(path, kind)
        for path, kind in zip(
            paths,
            (MediaKind.PHOTO, MediaKind.AUDIO, MediaKind.VIDEO, MediaKind.DOCUMENT),
            strict=True,
        )
    )
    job = Job("job", 1, 2, "https://example.com", "key", source_message_id=5)
    await gateway.deliver(job, artifacts, UserPreferences(delete_source=True))
    names = [call[0] for call in bot.calls]
    assert names == [
        "send_photo",
        "send_audio",
        "send_video",
        "send_document",
        "send_message",
    ]
    assert all(call[2].get("reply_markup") is None for call in bot.calls[:-1])
    assert all(call[2].get("caption") is None for call in bot.calls[:-1])
    assert bot.calls[-1][2]["reply_markup"] is not None
    uploads = [call[1][1] for call in bot.calls[:-1]]
    assert_uuid_filename(uploads[0].filename, ".jpg")
    assert uploads[1].filename == "file.mp3"
    assert_uuid_filename(uploads[2].filename, ".mp4")
    assert_uuid_filename(uploads[3].filename, ".bin")
    assert [upload.path for upload in uploads] == paths

    bot.calls.clear()
    await gateway.deliver(
        replace(job, inline_message_id="inline"),
        (artifacts[0],),
        UserPreferences(show_buttons=True),
    )
    assert [call[0] for call in bot.calls] == [
        "send_photo",
        "send_message",
        "edit_message_text",
    ]


@pytest.mark.asyncio
async def test_gateway_deletes_source_immediately() -> None:
    bot = Bot()
    gateway = AiogramTelegramGateway(bot)
    await gateway.delete_source(2, 5, "business")
    assert [call[0] for call in bot.calls] == ["delete_message"]


@pytest.mark.asyncio
async def test_gateway_sends_audio_collections_in_maximum_sized_media_groups(
    tmp_path,
) -> None:
    bot = Bot()
    gateway = AiogramTelegramGateway(bot)
    artifacts = []
    for index in range(12):
        path = tmp_path / f"track-{index}.mp3"
        path.write_bytes(b"x")
        artifacts.append(artifact(path, MediaKind.AUDIO))
    job = Job("album", 1, 2, "https://eu.hitmoz.com/album/532028", "key")
    await gateway.deliver(job, tuple(artifacts), UserPreferences())
    groups = [call for call in bot.calls if call[0] == "send_media_group"]
    assert [len(call[2]["media"]) for call in groups] == [10, 2]
    assert all(
        item.caption is None for group in groups for item in group[2]["media"]
    )
    actions = [call for call in bot.calls if call[0] == "send_message"]
    source_buttons = [
        button
        for row in actions[-1][2]["reply_markup"].inline_keyboard
        for button in row
        if button.text == "🔗 Source"
    ]
    assert source_buttons[0].url == job.source_url
    assert groups[1][2]["media"][0].caption is None
    assert [
        item.media.filename for group in groups for item in group[2]["media"]
    ] == [f"track-{index}.mp3" for index in range(12)]


@pytest.mark.asyncio
async def test_gateway_uses_uuid_filenames_for_non_audio_media_groups(tmp_path) -> None:
    photo = tmp_path / "source-title.jpg"
    video = tmp_path / "source-title.mp4"
    photo.write_bytes(b"photo")
    video.write_bytes(b"video")
    artifacts = (
        artifact(photo, MediaKind.PHOTO),
        artifact(video, MediaKind.VIDEO),
    )
    bot = Bot()

    await AiogramTelegramGateway(bot).deliver(
        Job("album", 1, 2, "https://example.com/post", "key"),
        artifacts,
        UserPreferences(show_buttons=False),
    )

    media = bot.calls[0][2]["media"]
    assert bot.calls[0][0] == "send_media_group"
    assert_uuid_filename(media[0].media.filename, ".jpg")
    assert_uuid_filename(media[1].media.filename, ".mp4")
    assert media[0].media.path == photo
    assert media[1].media.path == video


@pytest.mark.asyncio
async def test_gateway_sends_audio_metadata_instead_of_a_raw_filename(tmp_path) -> None:
    path = tmp_path / "opaque-cache-name.m4a"
    path.write_bytes(b"audio")
    thumbnail = tmp_path / "cover.jpg"
    thumbnail.write_bytes(b"jpeg")
    item = DownloadArtifact(
        PurePosixPath(path),
        MediaKind.AUDIO,
        5,
        "sum",
        "audio/mp4",
        title="Track",
        author="Artist",
        duration_ms=123_400,
        thumbnail_path=PurePosixPath(thumbnail),
    )
    bot = Bot()
    await AiogramTelegramGateway(bot).deliver(
        Job("job", 1, 2, "https://open.spotify.com/track/x", "key"),
        (item,),
        UserPreferences(),
    )
    kwargs = bot.calls[0][2]
    assert kwargs["title"] == "Track"
    assert kwargs["performer"] == "Artist"
    assert kwargs["duration"] == 123
    assert kwargs["thumbnail"] is not None
    assert "reply_markup" not in kwargs
    action_kwargs = bot.calls[1][2]
    labels = [
        button.text
        for row in action_kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "🎬 Get video" not in labels


@pytest.mark.asyncio
async def test_gateway_manual_retry_supports_chat_and_inline() -> None:
    bot = Bot()
    gateway = AiogramTelegramGateway(bot)
    job = Job("job", 1, 2, "https://example.com", "key", stage=JobStage.DELIVERING)
    await gateway.show_manual_retry(job)
    await gateway.show_manual_retry(replace(job, inline_message_id="inline"))
    assert [call[0] for call in bot.calls] == ["send_message", "edit_message_text"]


def test_gateway_keyboard_and_media_strategies() -> None:
    job = Job("job", 1, 2, "https://example.com", "key")
    assert len(_status_keyboard(job).inline_keyboard) == 2
    assert _status_keyboard(replace(job, cancel_requested=True)) is None
    assert _status_keyboard(replace(job, stage=JobStage.DELIVERED)) is None
    assert _source_keyboard(job.source_url).inline_keyboard[0][0].url == job.source_url
    prefs = UserPreferences()
    media = [
        _input_media(artifact("/tmp/x.jpg", MediaKind.PHOTO), prefs),
        _input_media(artifact("/tmp/x.mp3", MediaKind.AUDIO), prefs),
        _input_media(artifact("/tmp/x.mp4", MediaKind.VIDEO), prefs),
    ]
    assert isinstance(media[0], InputMediaPhoto)
    assert isinstance(media[1], InputMediaAudio)
    assert isinstance(media[2], InputMediaVideo)
    assert all(item.caption is None for item in media)
    assert isinstance(
        _input_media(
            artifact("/tmp/x.bin", MediaKind.VIDEO),
            replace(prefs, document_mode=True),
        ),
        InputMediaDocument,
    )
    eleven = tuple(
        artifact(f"/tmp/{index}.mp3", MediaKind.AUDIO) for index in range(11)
    )
    assert [len(group) for group in _chunks(eleven, 10)] == [9, 2]
    assert _can_send_media_groups(
        (
            artifact("/tmp/x.jpg", MediaKind.PHOTO),
            artifact("/tmp/x.mp4", MediaKind.VIDEO),
        ),
        prefs,
    )
