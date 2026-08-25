from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import replace
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from downloader_bot.adapters.telegram import AiogramTelegramGateway, build_router
from downloader_bot.adapters.telegram.access import InviteAccessMiddleware
from downloader_bot.application.progress import ProgressThrottle
from downloader_bot.domain import JobStage, Progress

from .container import Container, build_deliver
from .system import SystemClock

logger = logging.getLogger(__name__)


async def run_bot(container: Container) -> None:
    if not await container.access.has_admin():
        raise RuntimeError(
            "No administrator configured. Run: "
            "python -m downloader_bot admin add TELEGRAM_USER_ID"
        )
    session = AiohttpSession(
        api=TelegramAPIServer.from_base(container.settings.telegram_api_url)
    )
    bot = Bot(
        container.settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dispatcher = Dispatcher()
    bot_identity = await bot.get_me()
    gateway = AiogramTelegramGateway(bot, bot_username=bot_identity.username)
    deliver = build_deliver(container, gateway)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Open the downloader"),
            BotCommand(command="help", description="Formats, platforms, and limits"),
            BotCommand(command="settings", description="Choose download defaults"),
            BotCommand(command="status", description="View your downloads"),
            BotCommand(command="audio", description="Download a URL as audio"),
            BotCommand(command="video", description="Download a URL as video"),
            BotCommand(command="redeem", description="Unlock with an invite code"),
            BotCommand(command="admin", description="Manage invite access"),
        ]
    )
    router = build_router(
        container.submit,
        container.submit_batch,
        container.cancel,
        container.bind_inline,
        container.customize_job,
        deliver,
        container.manage_settings,
        container.get_stats,
        gateway,
        container.jobs,
        container.settings.admin_ids,
        create_selection=container.create_selection,
        update_selection=container.update_selection,
        confirm_selection=container.confirm_selection,
        get_user_jobs=container.get_user_jobs,
        retry_format=container.retry_format,
        bot_username=bot_identity.username,
        ux_selection_flow=container.settings.ux_selection_flow,
        ux_analytics=container.analytics,
        access_control=container.access,
        generate_invite=container.generate_invite,
        list_invites=container.list_invites,
        revoke_invite=container.revoke_invite,
    )
    access = InviteAccessMiddleware(
        container.check_access, container.redeem_invite, bot_identity.username
    )
    router.message.outer_middleware(access)
    router.business_message.outer_middleware(access)
    router.callback_query.outer_middleware(access)
    router.inline_query.outer_middleware(access)
    router.chosen_inline_result.outer_middleware(access)
    dispatcher.include_router(router)
    background = [
        asyncio.create_task(_outbox_loop(container), name="outbox-publisher"),
        asyncio.create_task(
            _progress_loop(container, gateway, deliver), name="progress-presenter"
        ),
        asyncio.create_task(_heartbeat_loop(), name="heartbeat"),
        asyncio.create_task(_cleanup_loop(container), name="artifact-cleanup"),
    ]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await session.close()


async def run_worker(container: Container) -> None:
    for job_id in await container.jobs.outstanding():
        await container.queue.publish(job_id)
    while True:
        for message_id, job_id in await container.queue.reclaim(
            container.settings.worker_name,
            idle_ms=container.settings.reclaim_idle_ms,
        ):
            await _process_message(container, message_id, job_id)
        async for message_id, job_id in container.queue.consume(
            container.settings.worker_name
        ):
            await _process_message(container, message_id, job_id)


async def _process_message(container: Container, message_id: str, job_id: str) -> None:
    try:
        job = await container.process.execute(job_id)
        if job and job.stage is JobStage.RETRYING:
            await asyncio.sleep(min(60.0, (2 ** (job.attempt - 1)) + random.random()))
            await container.queue.publish(job.id)
        await container.queue.ack(message_id)
    except Exception:
        logger.exception(
            "Worker failed while processing job %s; leaving message pending", job_id
        )


async def _outbox_loop(container: Container) -> None:
    while True:
        try:
            published = await container.publish_outbox.execute()
            await asyncio.sleep(0.2 if published else 1.0)
        except Exception:
            logger.exception("Outbox publisher iteration failed")
            await asyncio.sleep(2)


async def _progress_loop(container: Container, gateway, deliver) -> None:
    throttles: dict[str, ProgressThrottle] = {}
    while True:
        async for progress in container.progress.consume("bot-1"):
            try:
                await _present_progress(
                    container, gateway, deliver, throttles, progress
                )
            except Exception:
                logger.exception(
                    "Failed to present progress for job %s", progress.job_id
                )


async def _present_progress(container, gateway, deliver, throttles, progress) -> None:
    job = await container.jobs.get(progress.job_id)
    if job is None:
        return
    target_job = job
    target_progress = progress
    if progress.stage is JobStage.QUEUED and not job.is_parent:
        progress = replace(
            progress, queue_position=await container.jobs.queue_position(job.id)
        )
    if job.parent_id:
        parent = await container.refresh_parent.execute(job.parent_id)
        if parent is not None:
            job = parent
            progress = Progress(
                job_id=parent.id,
                stage=parent.stage,
                percent=progress.percent,
                attempt=progress.attempt,
                attempt_limit=progress.attempt_limit,
                item=progress.item,
                item_count=parent.children_total,
                queue_position=None,
                detail=progress.detail,
                error_code=progress.error_code,
                downloaded_bytes=progress.downloaded_bytes,
                total_bytes=progress.total_bytes,
                speed_bytes_per_second=progress.speed_bytes_per_second,
                eta_seconds=progress.eta_seconds,
            )
    throttle = throttles.setdefault(job.id, ProgressThrottle(SystemClock()))
    if throttle.accept(progress):
        status_id = await gateway.show_status(job, progress)
        if status_id and not job.status_message_id:
            await container.jobs.transition(
                job.id, {job.stage}, job.stage, status_message_id=status_id
            )
    if target_progress.stage is JobStage.READY and not target_job.is_parent:
        await deliver.execute(target_job.id)
    if target_progress.stage in {
        JobStage.DELIVERED,
        JobStage.CANCELLED,
        JobStage.FAILED,
    }:
        throttles.pop(target_job.id, None)


async def _cleanup_loop(container: Container) -> None:
    while True:
        try:
            await container.cleanup_artifacts.execute(
                retention_seconds=container.settings.artifact_retention_seconds
            )
        except Exception:
            logger.exception("Artifact cleanup iteration failed")
        await asyncio.sleep(300)


async def _heartbeat_loop(path: Path = Path("/tmp/bot_heartbeat")) -> None:
    while True:
        await asyncio.to_thread(path.write_text, str(time.time()), "utf-8")
        await asyncio.sleep(15)
