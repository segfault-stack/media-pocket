from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from downloader_bot.domain import (
    DownloadArtifact,
    Job,
    JobStage,
    MediaKind,
    Progress,
    SelectionRequest,
    UserPreferences,
)

from .presenter import (
    AUTO_DELETE_WARNING,
    DELIVERED_ITEMS_TEXT,
    INLINE_FALLBACK_TEXT,
    INLINE_OPEN_PRIVATE_TEXT,
    INLINE_SENT_TEXT,
    MANUAL_RETRY_TEXT,
    RESULT_ACTIONS_TEXT,
    failure_keyboard,
    render_progress,
    render_selection,
    selection_keyboard,
)

_MAX_MEDIA_GROUP_SIZE = 10


class AiogramTelegramGateway:
    def __init__(self, bot: Bot, *, bot_username: str | None = None) -> None:
        self._bot = bot
        self._bot_username = (bot_username or "").lstrip("@")
        self._delete_warnings: set[int] = set()

    async def show_selection(self, selection: SelectionRequest) -> int:
        message = await self._bot.send_message(
            selection.chat_id,
            render_selection(selection),
            reply_markup=selection_keyboard(selection),
            business_connection_id=selection.business_connection_id,
        )
        return message.message_id

    async def delete_source(
        self,
        chat_id: int,
        message_id: int,
        business_connection_id: str | None = None,
    ) -> None:
        try:
            await self._bot.delete_message(chat_id, message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            if chat_id not in self._delete_warnings:
                self._delete_warnings.add(chat_id)
                await self._bot.send_message(
                    chat_id,
                    AUTO_DELETE_WARNING,
                    business_connection_id=business_connection_id,
                )

    async def update_selection(self, selection: SelectionRequest) -> None:
        if not selection.status_message_id:
            return
        try:
            await self._bot.edit_message_text(
                render_selection(selection),
                chat_id=selection.chat_id,
                message_id=selection.status_message_id,
                reply_markup=selection_keyboard(selection),
                business_connection_id=selection.business_connection_id,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def show_status(self, job: Job, progress: Progress) -> int | None:
        if progress.stage is JobStage.DELIVERED and not job.inline_message_id:
            if job.status_message_id:
                with suppress(TelegramBadRequest):
                    await self._bot.delete_message(job.chat_id, job.status_message_id)
            return job.status_message_id
        markup = _status_keyboard(job)
        if progress.stage is JobStage.FAILED:
            markup = failure_keyboard(job.id)
        text = render_progress(progress, compact=job.preferences.compact_progress)
        if job.inline_message_id:
            try:
                await self._bot.edit_message_text(
                    text, inline_message_id=job.inline_message_id, reply_markup=markup
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            return None
        if job.status_message_id:
            try:
                await self._bot.edit_message_text(
                    text,
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    reply_markup=markup,
                    business_connection_id=job.business_connection_id,
                )
                return job.status_message_id
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return job.status_message_id
        message = await self._bot.send_message(
            job.chat_id,
            text,
            reply_markup=markup,
            business_connection_id=job.business_connection_id,
        )
        return message.message_id

    async def deliver(
        self,
        job: Job,
        artifacts: tuple[DownloadArtifact, ...],
        preferences: UserPreferences,
    ) -> bool:
        if job.inline_message_id:
            if not artifacts:
                return True
            private_job = replace(job, inline_message_id=None)
            try:
                if _can_send_media_groups(artifacts, preferences):
                    await self._deliver_media_groups(
                        private_job, artifacts, preferences
                    )
                else:
                    await self._deliver_individually(
                        private_job, artifacts, preferences
                    )
                await self._send_result_actions(private_job, artifacts, preferences)
            except (TelegramBadRequest, TelegramForbiddenError):
                await self._show_inline_fallback(job, ready=False)
                return False
            await self._show_inline_fallback(job, ready=True)
            return True
        if _can_send_media_groups(artifacts, preferences):
            await self._deliver_media_groups(job, artifacts, preferences)
        else:
            await self._deliver_individually(job, artifacts, preferences)
        await self._send_result_actions(job, artifacts, preferences)
        return True

    async def _show_inline_fallback(self, job: Job, *, ready: bool) -> None:
        if not job.inline_message_id:
            return
        markup = _inline_private_keyboard(job, self._bot_username)
        with suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                INLINE_SENT_TEXT if ready else INLINE_FALLBACK_TEXT,
                inline_message_id=job.inline_message_id,
                reply_markup=markup,
            )

    async def _deliver_media_groups(
        self,
        job: Job,
        artifacts: tuple[DownloadArtifact, ...],
        preferences: UserPreferences,
    ) -> None:
        for group in _chunks(artifacts, _MAX_MEDIA_GROUP_SIZE):
            media = [_input_media(artifact, preferences) for artifact in group]
            await self._bot.send_media_group(
                job.chat_id,
                media=media,
                business_connection_id=job.business_connection_id,
            )

    async def _send_result_actions(
        self,
        job: Job,
        artifacts: tuple[DownloadArtifact, ...],
        preferences: UserPreferences,
    ) -> None:
        if not preferences.show_buttons or not artifacts:
            return
        text = (
            DELIVERED_ITEMS_TEXT.format(count=len(artifacts))
            if len(artifacts) > 1
            else RESULT_ACTIONS_TEXT
        )
        await self._bot.send_message(
            job.chat_id,
            text,
            reply_markup=_result_keyboard(job, artifacts[0]),
            business_connection_id=job.business_connection_id,
        )

    async def _deliver_individually(
        self,
        job: Job,
        artifacts: tuple[DownloadArtifact, ...],
        preferences: UserPreferences,
    ) -> None:
        for artifact in artifacts:
            file = _upload_file(artifact)
            if preferences.document_mode or artifact.kind is MediaKind.DOCUMENT:
                await self._bot.send_document(
                    job.chat_id,
                    file,
                    business_connection_id=job.business_connection_id,
                )
            elif artifact.kind is MediaKind.PHOTO:
                await self._bot.send_photo(
                    job.chat_id,
                    file,
                    business_connection_id=job.business_connection_id,
                )
            elif artifact.kind is MediaKind.AUDIO:
                await self._bot.send_audio(
                    job.chat_id,
                    file,
                    title=artifact.title,
                    performer=artifact.author,
                    duration=_duration_seconds(artifact.duration_ms),
                    thumbnail=_thumbnail_file(artifact),
                    business_connection_id=job.business_connection_id,
                )
            else:
                await self._bot.send_video(
                    job.chat_id,
                    file,
                    supports_streaming=True,
                    thumbnail=_thumbnail_file(artifact),
                    business_connection_id=job.business_connection_id,
                )

    async def show_manual_retry(self, job: Job) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Send again", callback_data=f"job:deliver:{job.id}"
                    )
                ]
            ]
        )
        if job.inline_message_id:
            await self._bot.edit_message_text(
                MANUAL_RETRY_TEXT,
                inline_message_id=job.inline_message_id,
                reply_markup=markup,
            )
            return
        await self._bot.send_message(
            job.chat_id,
            MANUAL_RETRY_TEXT,
            reply_markup=markup,
            business_connection_id=job.business_connection_id,
        )


def _status_keyboard(job: Job) -> InlineKeyboardMarkup | None:
    if job.terminal or job.cancel_requested:
        return None
    rows = [
        [InlineKeyboardButton(text="✖️ Cancel", callback_data=f"job:cancel:{job.id}")]
    ]
    if job.stage.value == "queued" and not job.is_parent:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎧 Audio", callback_data=f"job:audio:{job.id}"
                ),
                InlineKeyboardButton(
                    text="📄 File", callback_data=f"job:document:{job.id}"
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _source_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔗 Source", url=url)]]
    )


def _result_keyboard(job: Job, artifact: DownloadArtifact) -> InlineKeyboardMarkup:
    rows = []
    if artifact.kind is MediaKind.AUDIO and _supports_video_source(job.source_url):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎬 Get video", callback_data=f"result:video:{job.id}"
                )
            ]
        )
    elif artifact.kind is MediaKind.VIDEO:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🎧 Get audio", callback_data=f"result:audio:{job.id}"
                )
            ]
        )
    if not job.preferences.document_mode:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📄 Send as file", callback_data=f"result:file:{job.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🔗 Source", url=job.source_url),
            InlineKeyboardButton(
                text="↗️ Share",
                url=f"https://t.me/share/url?url={quote(job.source_url, safe='')}",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _supports_video_source(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    audio_hosts = (
        "spotify.com",
        "soundcloud.com",
        "hitmoz.com",
        "hitmoz.me",
        "hitmos.me",
        "zaycev.net",
    )
    return not any(host == item or host.endswith(f".{item}") for item in audio_hosts)


def _inline_private_keyboard(job: Job, bot_username: str) -> InlineKeyboardMarkup:
    rows = []
    if bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=INLINE_OPEN_PRIVATE_TEXT,
                    url=f"https://t.me/{bot_username}?start=inline_{job.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔗 Source", url=job.source_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _can_send_media_groups(
    artifacts: tuple[DownloadArtifact, ...], preferences: UserPreferences
) -> bool:
    return (
        not preferences.document_mode
        and len(artifacts) >= 2
        and (
            all(artifact.kind is MediaKind.AUDIO for artifact in artifacts)
            or all(
                artifact.kind in {MediaKind.PHOTO, MediaKind.VIDEO}
                for artifact in artifacts
            )
        )
    )


def _chunks(
    items: tuple[DownloadArtifact, ...], size: int
) -> tuple[tuple[DownloadArtifact, ...], ...]:
    chunks = [items[offset : offset + size] for offset in range(0, len(items), size)]
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-1] = chunks[-2][-1:] + chunks[-1]
        chunks[-2] = chunks[-2][:-1]
    return tuple(chunks)


def _input_media(artifact: DownloadArtifact, preferences: UserPreferences):
    file = _upload_file(artifact)
    if preferences.document_mode or artifact.kind is MediaKind.DOCUMENT:
        return InputMediaDocument(media=file)
    if artifact.kind is MediaKind.PHOTO:
        return InputMediaPhoto(media=file)
    if artifact.kind is MediaKind.AUDIO:
        return InputMediaAudio(
            media=file,
            title=artifact.title,
            performer=artifact.author,
            duration=_duration_seconds(artifact.duration_ms),
            thumbnail=_thumbnail_file(artifact),
        )
    return InputMediaVideo(
        media=file,
        supports_streaming=True,
        thumbnail=_thumbnail_file(artifact),
    )


def _upload_file(artifact: DownloadArtifact) -> FSInputFile:
    path = Path(artifact.path)
    if artifact.kind is MediaKind.AUDIO:
        return FSInputFile(path)
    return FSInputFile(path, filename=f"{uuid4()}{path.suffix}")


def _duration_seconds(duration_ms: int | None) -> int | None:
    return round(duration_ms / 1_000) if duration_ms is not None else None


def _thumbnail_file(artifact: DownloadArtifact) -> FSInputFile | None:
    return FSInputFile(Path(artifact.thumbnail_path)) if artifact.thumbnail_path else None
