from __future__ import annotations

import re
from datetime import timedelta

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Message,
)

from downloader_bot.application.use_cases import SubmitDownloadCommand
from downloader_bot.domain import InviteKind, JobKind, JobStage, Progress

from .access import AdminOnlyMiddleware
from .presenter import (
    ACTION_UNAVAILABLE_TEXT,
    ADMIN_HOME_TEXT,
    ADMIN_INVITE_CREATED_TEXT,
    ADMIN_INVITE_REVOKED_TOAST,
    ADMIN_ONE_USE_DETAILS,
    ADMIN_TIMED_DETAILS,
    ALREADY_HANDLED_TEXT,
    ALREADY_STARTED_TEXT,
    AUDIO_SELECTED_TEXT,
    CANCELLATION_REQUESTED_TEXT,
    CANCELLED_TEXT,
    CANCELLED_TOAST,
    DOWNLOAD_STARTED_TEXT,
    EXPIRED_TEXT,
    FAST_AUDIO_HINT,
    FAST_VIDEO_HINT,
    FILE_SELECTED_TEXT,
    HELP_TEXT,
    INLINE_OPEN_PRIVATE_TEXT,
    INLINE_TITLE,
    INLINE_WAITING_TEXT,
    NO_PERMISSION_TEXT,
    NOT_OWNER_TEXT,
    SAVED_TEXT,
    START_TEXT,
    UNKNOWN_SETTING_TEXT,
    admin_invite_result_keyboard,
    admin_invites_keyboard,
    admin_invites_list_keyboard,
    inline_pending_keyboard,
    render_active_invites,
    render_stats,
    render_status,
    settings_home_keyboard,
    settings_home_text,
    settings_page,
    start_keyboard,
    status_keyboard,
)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_AUDIO_COMMAND_RE = re.compile(
    r"^\s*[!/](?:audio|a|mp3|music)(?:@[a-z0-9_]+)?(?:\s+|$)", re.IGNORECASE
)
_VIDEO_COMMAND_RE = re.compile(
    r"^\s*[!/](?:video|v)(?:@[a-z0-9_]+)?(?:\s+|$)", re.IGNORECASE
)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>\"'"


def build_router(
    submit,
    submit_batch,
    cancel,
    bind_inline,
    customize,
    deliver,
    settings,
    stats,
    gateway,
    jobs,
    admin_ids: frozenset[int] = frozenset(),
    *,
    create_selection=None,
    update_selection=None,
    confirm_selection=None,
    get_user_jobs=None,
    retry_format=None,
    bot_username: str | None = None,
    ux_selection_flow: bool = True,
    ux_analytics=None,
    access_control=None,
    generate_invite=None,
    list_invites=None,
    revoke_invite=None,
) -> Router:
    router = Router(name="downloads-v2")
    admin_router = Router(name="invite-admin")
    admin_only = AdminOnlyMiddleware(access_control, admin_ids)
    admin_router.message.outer_middleware(admin_only)
    admin_router.callback_query.outer_middleware(admin_only)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if (
            message.from_user
            and (text := getattr(message, "text", None))
            and (job_id := _inline_start_job(text))
        ):
            job = await jobs.route_inline_to_private(job_id, message.from_user.id)
            if job is not None:
                await deliver.execute(job.id, manual_retry=True)
                return
        await message.answer(START_TEXT, reply_markup=start_keyboard(bot_username))

    @router.message(Command("help"))
    async def show_help(message: Message) -> None:
        await _record(
            ux_analytics,
            "help_opened",
            message.from_user.id if message.from_user else None,
        )
        await message.answer(HELP_TEXT)

    @router.callback_query(F.data == "nav:help")
    async def callback_help(query: CallbackQuery) -> None:
        await _record(ux_analytics, "help_opened", query.from_user.id)
        if isinstance(query.message, Message):
            await query.message.edit_text(HELP_TEXT)
        await query.answer()

    @router.message(Command("settings"))
    async def show_settings(message: Message) -> None:
        if not message.from_user:
            return
        value = await settings.get(message.from_user.id)
        await _record(ux_analytics, "settings_opened", message.from_user.id)
        await message.answer(
            settings_home_text(value), reply_markup=settings_home_keyboard()
        )

    @router.callback_query(F.data == "nav:settings")
    async def callback_settings(query: CallbackQuery) -> None:
        value = await settings.get(query.from_user.id)
        await _record(ux_analytics, "settings_opened", query.from_user.id)
        if isinstance(query.message, Message):
            await query.message.edit_text(
                settings_home_text(value), reply_markup=settings_home_keyboard()
            )
        await query.answer()

    @router.callback_query(F.data.startswith("settings:"))
    async def update_settings(query: CallbackQuery) -> None:
        if not query.data:
            return
        parts = query.data.split(":")
        current = await settings.get(query.from_user.id)
        if parts[1] == "home":
            text, markup = settings_home_text(current), settings_home_keyboard()
        elif parts[1] == "page" and len(parts) == 3:
            text, markup = settings_page(current, parts[2])
        else:
            changes: dict[str, object] = {}
            if (
                parts[1] == "toggle"
                and len(parts) == 3
                and parts[2]
                in {
                    "show_buttons",
                    "delete_source",
                    "compact_progress",
                }
            ):
                changes[parts[2]] = not getattr(current, parts[2])
            elif (
                parts[1] == "set"
                and len(parts) == 4
                and parts[2]
                in {
                    "document_mode",
                    "default_audio_only",
                }
            ):
                changes[parts[2]] = parts[3] == "true"
            elif (
                parts[1] == "quality"
                and len(parts) == 3
                and parts[2]
                in {
                    "best",
                    "1080",
                    "720",
                    "480",
                }
            ):
                changes["quality"] = parts[2]
            else:
                await query.answer(UNKNOWN_SETTING_TEXT, show_alert=True)
                return
            updated = await settings.update(query.from_user.id, **changes)
            page = _settings_page_for(parts[2])
            text, markup = settings_page(updated, page)
        if isinstance(query.message, Message):
            await query.message.edit_text(text, reply_markup=markup)
        await query.answer(SAVED_TEXT)

    @router.message(Command("status"))
    async def show_status(message: Message) -> None:
        if not message.from_user or get_user_jobs is None:
            return
        values = await get_user_jobs.execute(message.from_user.id)
        await message.answer(
            render_status(values), reply_markup=status_keyboard(values)
        )

    @router.message(Command("stats"))
    async def show_stats(message: Message) -> None:
        values = await stats.execute()
        await message.answer(render_stats(values))

    @router.message(Command("admin"))
    async def show_admin(message: Message) -> None:
        if not message.from_user:
            return
        allowed = message.from_user.id in admin_ids
        if access_control is not None:
            allowed = await access_control.is_admin(message.from_user.id)
        if not allowed:
            await message.answer(NO_PERMISSION_TEXT)
            return
        await message.answer(ADMIN_HOME_TEXT, reply_markup=admin_invites_keyboard())

    @admin_router.callback_query(F.data == "adm:home")
    async def admin_home(query: CallbackQuery) -> None:
        if isinstance(query.message, Message):
            await query.message.edit_text(
                ADMIN_HOME_TEXT, reply_markup=admin_invites_keyboard()
            )
        await query.answer()

    @admin_router.callback_query(F.data.startswith("adm:new:"))
    async def admin_create_invite(query: CallbackQuery) -> None:
        if not query.data or generate_invite is None:
            return
        preset = query.data.rsplit(":", 1)[-1]
        lifetimes = {
            "24h": (timedelta(hours=24), "24 hours"),
            "7d": (timedelta(days=7), "7 days"),
            "30d": (timedelta(days=30), "30 days"),
        }
        if preset == "once":
            invite = await generate_invite.execute(
                query.from_user.id, InviteKind.ONE_TIME
            )
            details = ADMIN_ONE_USE_DETAILS
        elif preset in lifetimes:
            lifetime, label = lifetimes[preset]
            invite = await generate_invite.execute(
                query.from_user.id, InviteKind.TIMED, valid_for=lifetime
            )
            details = ADMIN_TIMED_DETAILS.format(duration=label)
        else:
            await query.answer(ACTION_UNAVAILABLE_TEXT, show_alert=True)
            return
        if isinstance(query.message, Message):
            await query.message.edit_text(
                ADMIN_INVITE_CREATED_TEXT.format(
                    code=invite.code, details=details
                ),
                reply_markup=admin_invite_result_keyboard(invite.code),
            )
        await query.answer()

    @admin_router.callback_query(F.data == "adm:list")
    async def admin_list_invites(query: CallbackQuery) -> None:
        if list_invites is None:
            return
        invites = await list_invites.execute(query.from_user.id)
        if isinstance(query.message, Message):
            await query.message.edit_text(
                render_active_invites(invites),
                reply_markup=admin_invites_list_keyboard(
                    [invite.code for invite in invites]
                ),
            )
        await query.answer()

    @admin_router.callback_query(F.data.startswith("adm:revoke:"))
    async def admin_revoke_invite(query: CallbackQuery) -> None:
        if not query.data or revoke_invite is None or list_invites is None:
            return
        await revoke_invite.execute(
            query.from_user.id, query.data.rsplit(":", 1)[-1]
        )
        invites = await list_invites.execute(query.from_user.id)
        if isinstance(query.message, Message):
            await query.message.edit_text(
                render_active_invites(invites),
                reply_markup=admin_invites_list_keyboard(
                    [invite.code for invite in invites]
                ),
            )
        await query.answer(ADMIN_INVITE_REVOKED_TOAST)

    @router.message(F.text)
    @router.business_message(F.text)
    async def links(message: Message) -> None:
        if not message.from_user or not message.text:
            return
        audio_only = _is_audio_command(message.text)
        video_only = _is_video_command(message.text)
        urls = _extract_urls(message.text)
        if not urls:
            if audio_only:
                await message.answer(FAST_AUDIO_HINT)
            elif video_only:
                await message.answer(FAST_VIDEO_HINT)
            return
        business_connection_id = getattr(message, "business_connection_id", None)
        kind = (
            JobKind.BUSINESS
            if business_connection_id
            else JobKind.GROUP_REPLAY
            if message.chat.type != ChatType.PRIVATE
            else JobKind.DIRECT
        )
        if (
            ux_selection_flow
            and create_selection is not None
            and not audio_only
            and not video_only
        ):
            selection = await create_selection.execute(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                urls=urls,
                kind=kind,
                source_message_id=message.message_id,
                business_connection_id=business_connection_id,
            )
            status_id = await gateway.show_selection(selection)
            await create_selection.bind_message(
                selection.token, message.from_user.id, status_id
            )
            await _delete_source_if_enabled(message, settings, gateway)
            return
        await _submit_fast(
            message,
            urls,
            audio_only,
            business_connection_id,
            submit,
            submit_batch,
            gateway,
            jobs,
        )
        await _delete_source_if_enabled(message, settings, gateway)

    @router.callback_query(F.data.startswith("sel:"))
    async def selection_action(query: CallbackQuery) -> None:
        if not query.data or update_selection is None or confirm_selection is None:
            return
        parts = query.data.split(":")
        action = parts[1]
        token = parts[-1]
        current = await update_selection.get(token)
        if current is not None and current.user_id != query.from_user.id:
            await query.answer(NOT_OWNER_TEXT, show_alert=True)
            return
        if current is not None and not current.active:
            await query.answer(ALREADY_HANDLED_TEXT, show_alert=True)
            return
        if action == "confirm":
            selection, job, claimed = await confirm_selection.execute(
                token, query.from_user.id
            )
            if not claimed or job is None:
                await update_selection.record_expired(query.from_user.id)
                await query.answer(EXPIRED_TEXT, show_alert=True)
                return
            await gateway.show_status(
                job,
                Progress(
                    job_id=job.id,
                    stage=JobStage.QUEUED,
                    item=0 if job.is_parent else 1,
                    item_count=job.children_total or 1,
                    queue_position=None
                    if job.is_parent
                    else await jobs.queue_position(job.id),
                ),
            )
            await query.answer(DOWNLOAD_STARTED_TEXT)
            return
        if action == "cancel":
            selection = await update_selection.cancel(token, query.from_user.id)
            if selection is None:
                await update_selection.record_expired(query.from_user.id)
                await query.answer(EXPIRED_TEXT, show_alert=True)
                return
            if isinstance(query.message, Message):
                await query.message.edit_text(CANCELLED_TEXT)
            await query.answer(CANCELLED_TOAST)
            return
        if len(parts) != 4:
            await query.answer(EXPIRED_TEXT, show_alert=True)
            return
        selection = await update_selection.execute(
            token, query.from_user.id, action=action, value=parts[2]
        )
        if selection is None:
            await update_selection.record_expired(query.from_user.id)
            await query.answer(EXPIRED_TEXT, show_alert=True)
            return
        await gateway.update_selection(selection)
        await query.answer(SAVED_TEXT)

    @router.callback_query(F.data.startswith("job:cancel:"))
    async def cancel_job(query: CallbackQuery) -> None:
        job = None
        if query.data:
            job = await cancel.execute(
                query.data.rsplit(":", 1)[-1], query.from_user.id
            )
        if job:
            await gateway.show_status(
                job,
                Progress(
                    job_id=job.id,
                    stage=job.stage,
                    percent=100 if job.terminal else 0,
                    item_count=job.children_total or 1,
                    error_code=job.error_code,
                ),
            )
        await query.answer(CANCELLATION_REQUESTED_TEXT if job else NOT_OWNER_TEXT)

    @router.callback_query(F.data.startswith("job:deliver:"))
    async def retry_delivery(query: CallbackQuery) -> None:
        if query.data:
            await deliver.execute(query.data.rsplit(":", 1)[-1], manual_retry=True)
        await query.answer()

    @router.callback_query(F.data.startswith("job:audio:"))
    async def request_audio(query: CallbackQuery) -> None:
        if not query.data:
            return
        job = await customize.audio(query.data.rsplit(":", 1)[-1], query.from_user.id)
        await query.answer(AUDIO_SELECTED_TEXT if job else ALREADY_STARTED_TEXT)

    @router.callback_query(F.data.startswith("job:document:"))
    async def request_document(query: CallbackQuery) -> None:
        if not query.data:
            return
        job = await customize.document(
            query.data.rsplit(":", 1)[-1], query.from_user.id
        )
        await query.answer(FILE_SELECTED_TEXT if job else ALREADY_STARTED_TEXT)

    @router.callback_query(F.data.startswith("result:"))
    async def result_action(query: CallbackQuery) -> None:
        if not query.data:
            return
        _, action, job_id = query.data.split(":", 2)
        source = await jobs.get(job_id)
        if source is None or source.user_id != query.from_user.id:
            await query.answer(NOT_OWNER_TEXT, show_alert=True)
            return
        if action == "format" and create_selection is not None:
            selection = await create_selection.execute(
                user_id=source.user_id,
                chat_id=source.chat_id,
                urls=(source.source_url,),
                kind=source.kind,
                business_connection_id=source.business_connection_id,
            )
            status_id = await gateway.show_selection(selection)
            await create_selection.bind_message(
                selection.token, query.from_user.id, status_id
            )
            await query.answer()
            return
        if retry_format is None:
            return
        job = await retry_format.execute(job_id, query.from_user.id, action)
        if job:
            status_id = await gateway.show_status(
                job,
                Progress(
                    job.id,
                    JobStage.QUEUED,
                    queue_position=await jobs.queue_position(job.id),
                ),
            )
            if status_id and not job.status_message_id:
                await jobs.transition(
                    job.id,
                    {JobStage.QUEUED},
                    JobStage.QUEUED,
                    status_message_id=status_id,
                )
        await query.answer(DOWNLOAD_STARTED_TEXT if job else ACTION_UNAVAILABLE_TEXT)

    @router.inline_query()
    async def inline(query: InlineQuery) -> None:
        urls = _extract_urls(query.query)
        if urls:
            if create_selection is not None:
                selection = await create_selection.execute(
                    user_id=query.from_user.id,
                    chat_id=query.from_user.id,
                    urls=(urls[0],),
                    kind=JobKind.INLINE,
                )
                result = InlineQueryResultArticle(
                    id=f"inline:{selection.token}",
                    title=INLINE_TITLE,
                    description=query.query[:100],
                    input_message_content=InputTextMessageContent(
                        message_text=INLINE_WAITING_TEXT
                    ),
                    reply_markup=inline_pending_keyboard(selection.token),
                )
                await query.answer([result], cache_time=1, is_personal=True)
                return
            job, _ = await submit.execute(
                SubmitDownloadCommand(
                    user_id=query.from_user.id,
                    chat_id=query.from_user.id,
                    url=urls[0],
                    kind=JobKind.INLINE,
                )
            )
            result = InlineQueryResultArticle(
                id=f"job:{job.id}",
                title=INLINE_TITLE,
                description=query.query[:100],
                input_message_content=InputTextMessageContent(
                    message_text=INLINE_WAITING_TEXT
                ),
            )
            await query.answer([result], cache_time=1, is_personal=True)
            return
        await query.answer(
            [],
            cache_time=1,
            is_personal=True,
            button=InlineQueryResultsButton(
                text=INLINE_OPEN_PRIVATE_TEXT, start_parameter="inline"
            ),
        )

    @router.callback_query(F.data.startswith("inline:pending:"))
    async def inline_pending(query: CallbackQuery) -> None:
        await query.answer(INLINE_WAITING_TEXT)

    @router.chosen_inline_result()
    async def chosen_inline(result: ChosenInlineResult) -> None:
        if not result.inline_message_id:
            return
        if result.result_id.startswith("inline:") and confirm_selection is not None:
            _, job, claimed = await confirm_selection.execute(
                result.result_id.removeprefix("inline:"),
                result.from_user.id,
                inline_message_id=result.inline_message_id,
            )
            if claimed and job and job.stage is JobStage.READY:
                await deliver.execute(job.id)
            return
        if not result.result_id.startswith("job:"):
            return
        job = await bind_inline.execute(
            result.result_id.removeprefix("job:"),
            result.from_user.id,
            result.inline_message_id,
        )
        if job and job.stage is JobStage.READY:
            await deliver.execute(job.id)

    router.include_router(admin_router)
    return router


async def _submit_fast(
    message,
    urls,
    audio_only,
    business_connection_id,
    submit,
    submit_batch,
    gateway,
    jobs,
) -> None:
    kind = (
        JobKind.BUSINESS
        if business_connection_id
        else JobKind.GROUP_REPLAY
        if message.chat.type != ChatType.PRIVATE
        else JobKind.DIRECT
    )
    if len(urls) > 1:
        job, _ = await submit_batch.execute(
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            urls=urls,
            source_message_id=message.message_id,
            business_connection_id=business_connection_id,
            audio_only=audio_only,
        )
    else:
        job, _ = await submit.execute(
            SubmitDownloadCommand(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                url=urls[0],
                kind=kind,
                source_message_id=message.message_id,
                business_connection_id=business_connection_id,
                audio_only=audio_only,
            )
        )
    status_id = await gateway.show_status(
        job,
        Progress(
            job_id=job.id,
            stage=JobStage.QUEUED,
            item=0 if job.is_parent else 1,
            item_count=job.children_total or 1,
            queue_position=None if job.is_parent else await jobs.queue_position(job.id),
        ),
    )
    if status_id and not job.status_message_id:
        await jobs.transition(
            job.id, {JobStage.QUEUED}, JobStage.QUEUED, status_message_id=status_id
        )


async def _delete_source_if_enabled(message, settings, gateway) -> None:
    preferences = await settings.get(message.from_user.id)
    if preferences.delete_source:
        await gateway.delete_source(
            message.chat.id,
            message.message_id,
            getattr(message, "business_connection_id", None),
        )


def _is_audio_command(text: str | None) -> bool:
    return bool(text and _AUDIO_COMMAND_RE.match(text))


def _is_video_command(text: str | None) -> bool:
    return bool(text and _VIDEO_COMMAND_RE.match(text))


def _extract_urls(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(url.rstrip(_TRAILING_URL_PUNCTUATION) for url in URL_RE.findall(text))


def _settings_page_for(field: str) -> str:
    if field in {"quality", "default_audio_only"}:
        return "download"
    if field in {"document_mode", "show_buttons"}:
        return "delivery"
    return "chat"


def _inline_start_job(text: str) -> str | None:
    match = re.match(
        r"^/start(?:@[a-z0-9_]+)?\s+inline_([a-z0-9-]+)$", text, re.IGNORECASE
    )
    return match.group(1) if match else None


async def _record(analytics, event: str, user_id: int | None) -> None:
    if analytics is not None:
        await analytics.record(event, user_id=user_id)
