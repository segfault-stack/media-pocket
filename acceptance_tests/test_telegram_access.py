from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from downloader_bot.adapters.telegram import access as access_module
from downloader_bot.adapters.telegram.access import InviteAccessMiddleware, _invite_code
from downloader_bot.adapters.telegram.presenter import (
    admin_invite_result_keyboard,
    render_active_invites,
)
from downloader_bot.adapters.telegram.router import build_router
from downloader_bot.domain import InviteCode, InviteKind, InviteRedemption


class Check:
    def __init__(self, allowed: bool = False) -> None:
        self.allowed = allowed
        self.calls: list[int] = []

    async def execute(self, user_id: int) -> bool:
        self.calls.append(user_id)
        return self.allowed


class Redeem:
    def __init__(self, result: InviteRedemption) -> None:
        self.result = result
        self.calls: list[tuple[int, str]] = []

    async def execute(self, user_id: int, code: str) -> InviteRedemption:
        self.calls.append((user_id, code))
        return self.result


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.text = text
        self.answer = AsyncMock()


class FakeInlineQuery:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=42)
        self.answer = AsyncMock()


class FakeChosenInlineResult:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=42)


class AccessControl:
    def __init__(self, admin: bool) -> None:
        self.admin = admin

    async def is_admin(self, _user_id: int) -> bool:
        return self.admin


class Generate:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, admin_id, kind, **kwargs):
        self.calls.append((admin_id, kind, kwargs))
        now = datetime(2026, 8, 25, tzinfo=UTC)
        return InviteCode(
            "JOIN2345",
            kind,
            admin_id,
            now,
            expires_at=now + kwargs["valid_for"]
            if kind is InviteKind.TIMED
            else None,
            max_uses=1 if kind is InviteKind.ONE_TIME else None,
        )


class InviteList:
    def __init__(self, values=()) -> None:
        self.values = values

    async def execute(self, _admin_id):
        return self.values


class Revoke:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, admin_id, code):
        self.calls.append((admin_id, code))
        return True


class GenericStub:
    async def execute(self, *_args, **_kwargs):
        return None

    async def get(self, *_args):
        return None


def _handler(router, observer: str, name: str):
    return next(
        item.callback
        for item in router.observers[observer].handlers
        if item.callback.__name__ == name
    )


def _admin_router(access_control, generate=None, invites=None, revoke=None):
    stub = GenericStub()
    router = build_router(
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        stub,
        access_control=access_control,
        generate_invite=generate,
        list_invites=invites,
        revoke_invite=revoke,
    )
    return router, router.sub_routers[0]


@pytest.mark.asyncio
async def test_access_middleware_redeems_code_before_any_download_handler(
    monkeypatch,
) -> None:
    monkeypatch.setattr(access_module, "Message", FakeMessage)
    check = Check()
    redeem = Redeem(InviteRedemption.ACCEPTED)
    middleware = InviteAccessMiddleware(check, redeem, "DownloaderBot")
    handler = AsyncMock()
    message = FakeMessage("join2345")

    await middleware(handler, message, {"event_from_user": message.from_user})

    assert redeem.calls == [(42, "JOIN2345")]
    handler.assert_not_awaited()
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_access_middleware_blocks_inline_and_cached_chosen_result(
    monkeypatch,
) -> None:
    monkeypatch.setattr(access_module, "InlineQuery", FakeInlineQuery)
    monkeypatch.setattr(
        access_module, "ChosenInlineResult", FakeChosenInlineResult
    )
    middleware = InviteAccessMiddleware(
        Check(), Redeem(InviteRedemption.INVALID), "DownloaderBot"
    )
    handler = AsyncMock()
    query = FakeInlineQuery()

    await middleware(handler, query, {"event_from_user": query.from_user})
    await middleware(
        handler,
        FakeChosenInlineResult(),
        {"event_from_user": SimpleNamespace(id=42)},
    )

    handler.assert_not_awaited()
    query.answer.assert_awaited_once()
    kwargs = query.answer.await_args.kwargs
    assert kwargs["is_personal"] is True
    assert kwargs["cache_time"] == 1
    assert kwargs["button"].start_parameter == "invite"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (InviteRedemption.INVALID, "invalid"),
        (InviteRedemption.REVOKED, "invalid"),
        (InviteRedemption.EXPIRED, "expired"),
        (InviteRedemption.USED, "already been used"),
    ],
)
async def test_access_middleware_explains_rejected_invites(
    monkeypatch, result, expected
) -> None:
    monkeypatch.setattr(access_module, "Message", FakeMessage)
    message = FakeMessage("JOIN2345")
    await InviteAccessMiddleware(Check(), Redeem(result), "DownloaderBot")(
        AsyncMock(), message, {"event_from_user": message.from_user}
    )
    assert expected in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_access_middleware_blocks_callbacks(monkeypatch) -> None:
    class FakeCallback:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=42)
            self.answer = AsyncMock()

    monkeypatch.setattr(access_module, "CallbackQuery", FakeCallback)
    callback = FakeCallback()
    handler = AsyncMock()
    await InviteAccessMiddleware(Check(), Redeem(InviteRedemption.INVALID), None)(
        handler, callback, {"event_from_user": callback.from_user}
    )
    handler.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_authorized_inline_user_reaches_handler() -> None:
    middleware = InviteAccessMiddleware(
        Check(allowed=True), Redeem(InviteRedemption.INVALID), "DownloaderBot"
    )
    handler = AsyncMock(return_value="ok")
    event = SimpleNamespace(from_user=SimpleNamespace(id=42))

    assert await middleware(handler, event, {"event_from_user": event.from_user}) == "ok"
    handler.assert_awaited_once()


def test_invite_input_and_copy_button_are_short_and_explicit() -> None:
    assert _invite_code("/redeem abc234") == "ABC234"
    assert _invite_code("/start JOIN2345") == "JOIN2345"
    assert _invite_code("/start invite") is None
    assert _invite_code("/start inline_job-id") is None
    assert _invite_code("/settings") is None
    button = admin_invite_result_keyboard("JOIN2345").inline_keyboard[0][0]
    assert button.text == "📋 Copy code"
    assert button.copy_text and button.copy_text.text == "JOIN2345"


@pytest.mark.asyncio
async def test_admin_router_creates_lists_and_revokes_invites(monkeypatch) -> None:
    from downloader_bot.adapters.telegram import router as router_module

    monkeypatch.setattr(router_module, "Message", FakeMessage)
    generate, revoke = Generate(), Revoke()
    now = datetime(2026, 8, 25, tzinfo=UTC)
    active = InviteCode(
        "JOIN2345",
        InviteKind.TIMED,
        42,
        now,
        expires_at=now + timedelta(days=7),
    )
    invites = InviteList((active,))
    root, admin = _admin_router(AccessControl(True), generate, invites, revoke)
    message = FakeMessage("/admin")
    await _handler(root, "message", "show_admin")(message)
    message.edit_text = AsyncMock()
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        data="adm:new:once",
        message=message,
        answer=AsyncMock(),
    )
    await _handler(admin, "callback_query", "admin_create_invite")(query)
    query.data = "adm:new:7d"
    await _handler(admin, "callback_query", "admin_create_invite")(query)
    await _handler(admin, "callback_query", "admin_list_invites")(query)
    query.data = "adm:revoke:JOIN2345"
    await _handler(admin, "callback_query", "admin_revoke_invite")(query)
    query.data = "adm:home"
    await _handler(admin, "callback_query", "admin_home")(query)

    assert generate.calls[0][1] is InviteKind.ONE_TIME
    assert generate.calls[1][2]["valid_for"] == timedelta(days=7)
    assert revoke.calls == [(42, "JOIN2345")]
    assert message.edit_text.await_count == 5


@pytest.mark.asyncio
async def test_admin_command_rejects_non_admin() -> None:
    root, _admin = _admin_router(AccessControl(False))
    message = FakeMessage("/admin")
    await _handler(root, "message", "show_admin")(message)
    assert "permission" in message.answer.await_args.args[0]


def test_active_invite_presenter_handles_empty_and_one_use() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    once = InviteCode(
        "ONCE2345", InviteKind.ONE_TIME, 42, now, max_uses=1
    )
    assert "No active invites" in render_active_invites(())
    assert "0/1 used" in render_active_invites((once,))
