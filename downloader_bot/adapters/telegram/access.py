from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResultsButton,
    Message,
    TelegramObject,
)

from downloader_bot.domain import InviteRedemption

from .presenter import (
    ACCESS_GRANTED_TEXT,
    ACCESS_REQUIRED_TEXT,
    EXPIRED_INVITE_TEXT,
    INLINE_ACCESS_BUTTON,
    INVALID_INVITE_TEXT,
    NO_PERMISSION_TEXT,
    USED_INVITE_TEXT,
)

_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")


class InviteAccessMiddleware(BaseMiddleware):
    """Stop every Telegram entry point until its sender has redeemed an invite."""

    def __init__(self, check_access, redeem_invite, bot_username: str | None) -> None:
        self._check_access = check_access
        self._redeem_invite = redeem_invite
        self._bot_username = bot_username

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        if user is None or await self._check_access.execute(user.id):
            return await handler(event, data)
        if isinstance(event, Message):
            code = _invite_code(getattr(event, "text", None))
            if code:
                result = await self._redeem_invite.execute(user.id, code)
                if result in {
                    InviteRedemption.ACCEPTED,
                    InviteRedemption.ALREADY_AUTHORIZED,
                }:
                    await event.answer(ACCESS_GRANTED_TEXT)
                    return None
                text = (
                    EXPIRED_INVITE_TEXT
                    if result is InviteRedemption.EXPIRED
                    else USED_INVITE_TEXT
                    if result is InviteRedemption.USED
                    else INVALID_INVITE_TEXT
                )
                await event.answer(text)
                return None
            await event.answer(ACCESS_REQUIRED_TEXT)
            return None
        if isinstance(event, CallbackQuery):
            await event.answer(ACCESS_REQUIRED_TEXT, show_alert=True)
            return None
        if isinstance(event, InlineQuery):
            await event.answer(
                [],
                cache_time=1,
                is_personal=True,
                button=InlineQueryResultsButton(
                    text=INLINE_ACCESS_BUTTON, start_parameter="invite"
                ),
            )
            return None
        if isinstance(event, ChosenInlineResult):
            return None
        return None


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, access_control, fallback_ids: frozenset[int]) -> None:
        self._access_control = access_control
        self._fallback_ids = fallback_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        allowed = bool(user and user.id in self._fallback_ids)
        if user is not None and self._access_control is not None:
            allowed = await self._access_control.is_admin(user.id)
        if allowed:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer(NO_PERMISSION_TEXT, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(NO_PERMISSION_TEXT)
        return None


def _invite_code(text: str | None) -> str | None:
    value = (text or "").strip()
    parts = value.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if len(parts) == 2 and command in {"/redeem", "/start"}:
        value = parts[1].strip()
        if command == "/start" and (
            value.lower() == "invite" or value.lower().startswith("inline_")
        ):
            return None
    elif value.startswith("/"):
        return None
    return value.upper() if _CODE_RE.fullmatch(value) else None
