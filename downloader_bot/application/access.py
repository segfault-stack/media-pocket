from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import timedelta

from downloader_bot.domain import InviteCode, InviteKind, InviteRedemption

from .ports import AccessRepository, Clock

INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MIN_LIMITED_INVITE_USES = 2
MAX_LIMITED_INVITE_USES = 100_000


def generate_invite_code(length: int = 8) -> str:
    if length < 4:
        raise ValueError("invite code length must be at least 4")
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(length))


class CheckAccess:
    def __init__(self, access: AccessRepository) -> None:
        self._access = access

    async def execute(self, user_id: int) -> bool:
        return await self._access.is_allowed(user_id)


class GenerateInvite:
    def __init__(
        self,
        access: AccessRepository,
        clock: Clock,
        code_factory: Callable[[], str] = generate_invite_code,
    ) -> None:
        self._access = access
        self._clock = clock
        self._code_factory = code_factory

    async def execute(
        self,
        admin_id: int,
        kind: InviteKind,
        *,
        valid_for: timedelta | None = None,
        max_uses: int | None = None,
    ) -> InviteCode:
        if not await self._access.is_admin(admin_id):
            raise PermissionError("administrator access required")
        if kind is InviteKind.TIMED:
            if valid_for is None or valid_for <= timedelta(0) or max_uses is not None:
                raise ValueError("timed invites require a positive lifetime")
        elif kind is InviteKind.ONE_TIME and (
            valid_for is not None or max_uses is not None
        ):
            raise ValueError("one-time invites do not accept a lifetime")
        elif kind is InviteKind.LIMITED and (
            valid_for is not None
            or max_uses is None
            or not MIN_LIMITED_INVITE_USES
            <= max_uses
            <= MAX_LIMITED_INVITE_USES
        ):
            raise ValueError("limited invite use count must be between 2 and 100000")

        now = self._clock.now()
        for _ in range(10):
            code = self._code_factory().strip().upper()
            invite = InviteCode(
                code=code,
                kind=kind,
                created_by=admin_id,
                created_at=now,
                expires_at=now + valid_for
                if kind is InviteKind.TIMED and valid_for is not None
                else None,
                max_uses=1 if kind is InviteKind.ONE_TIME else max_uses,
            )
            if await self._access.create_invite(invite):
                return invite
        raise RuntimeError("could not allocate a unique invite code")


class RedeemInvite:
    def __init__(self, access: AccessRepository, clock: Clock) -> None:
        self._access = access
        self._clock = clock

    async def execute(self, user_id: int, code: str) -> InviteRedemption:
        normalized = code.strip().upper()
        if not normalized or not normalized.isascii() or not normalized.isalnum():
            return InviteRedemption.INVALID
        return await self._access.redeem_invite(normalized, user_id, self._clock.now())


class ListInvites:
    def __init__(self, access: AccessRepository, clock: Clock) -> None:
        self._access = access
        self._clock = clock

    async def execute(self, admin_id: int) -> tuple[InviteCode, ...]:
        if not await self._access.is_admin(admin_id):
            raise PermissionError("administrator access required")
        return await self._access.list_invites(admin_id, self._clock.now())


class RevokeInvite:
    def __init__(self, access: AccessRepository, clock: Clock) -> None:
        self._access = access
        self._clock = clock

    async def execute(self, admin_id: int, code: str) -> bool:
        if not await self._access.is_admin(admin_id):
            raise PermissionError("administrator access required")
        return await self._access.revoke_invite(
            code.strip().upper(), admin_id, self._clock.now()
        )
