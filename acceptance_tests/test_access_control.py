from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from downloader_bot.application.access import (
    CheckAccess,
    GenerateInvite,
    ListInvites,
    RedeemInvite,
    RevokeInvite,
    generate_invite_code,
)
from downloader_bot.domain import InviteCode, InviteKind, InviteRedemption
from downloader_bot.infrastructure.database import Base, _invite, _invite_values

TEST_ADMIN_ID = 123_456_789


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, 9, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return 0.0


class Access:
    def __init__(self) -> None:
        self.admins = {TEST_ADMIN_ID}
        self.allowed: set[int] = set()
        self.invites: dict[str, InviteCode] = {}
        self.create_attempts = 0

    async def add_admin(self, user_id, *, added_by=None):
        created = user_id not in self.admins
        self.admins.add(user_id)
        return created

    async def has_admin(self):
        return bool(self.admins)

    async def is_admin(self, user_id):
        return user_id in self.admins

    async def is_allowed(self, user_id):
        return user_id in self.admins or user_id in self.allowed

    async def create_invite(self, invite):
        self.create_attempts += 1
        if invite.code in self.invites:
            return False
        self.invites[invite.code] = invite
        return True

    async def redeem_invite(self, code, user_id, now):
        if await self.is_allowed(user_id):
            return InviteRedemption.ALREADY_AUTHORIZED
        invite = self.invites.get(code)
        if invite is None:
            return InviteRedemption.INVALID
        if invite.revoked_at:
            return InviteRedemption.REVOKED
        if invite.expires_at and invite.expires_at <= now:
            return InviteRedemption.EXPIRED
        if invite.max_uses is not None and invite.use_count >= invite.max_uses:
            return InviteRedemption.USED
        self.invites[code] = replace(invite, use_count=invite.use_count + 1)
        self.allowed.add(user_id)
        return InviteRedemption.ACCEPTED

    async def list_invites(self, created_by, now):
        return tuple(
            x
            for x in self.invites.values()
            if x.created_by == created_by and x.available_at(now)
        )

    async def revoke_invite(self, code, admin_id, now):
        if admin_id not in self.admins or code not in self.invites:
            return False
        self.invites[code] = replace(self.invites[code], revoked_at=now)
        return True


def test_invite_domain_enforces_each_policy() -> None:
    now = datetime.now(UTC)
    timed = InviteCode("ABC123", InviteKind.TIMED, 1, now, now + timedelta(hours=1))
    once = InviteCode("XYZ789", InviteKind.ONE_TIME, 1, now, max_uses=1)
    limited = InviteCode("LIMIT234", InviteKind.LIMITED, 1, now, max_uses=25)
    assert timed.available_at(now)
    assert once.available_at(now)
    assert limited.available_at(now)
    with pytest.raises(ValueError, match="timed invites require"):
        InviteCode("BAD123", InviteKind.TIMED, 1, now)
    with pytest.raises(ValueError, match="one-time invites require"):
        InviteCode("BAD456", InviteKind.ONE_TIME, 1, now, max_uses=2)
    with pytest.raises(ValueError, match="limited invites require"):
        InviteCode("BAD789", InviteKind.LIMITED, 1, now, max_uses=1)


def test_generated_codes_are_short_unambiguous_alphanumeric() -> None:
    codes = {generate_invite_code() for _ in range(20)}
    assert all(len(code) == 8 and code.isascii() and code.isalnum() for code in codes)
    assert all(not ({"0", "1", "I", "O"} & set(code)) for code in codes)


@pytest.mark.asyncio
async def test_admin_generates_all_invite_policies_with_collision_retry() -> None:
    access = Access()
    access.invites["DUPL2345"] = InviteCode(
        "DUPL2345", InviteKind.ONE_TIME, TEST_ADMIN_ID, Clock().now(), max_uses=1
    )
    values = iter(("DUPL2345", "GOOD2345", "ONCE2345", "LIMIT234"))
    generate = GenerateInvite(access, Clock(), lambda: next(values))
    timed = await generate.execute(
        TEST_ADMIN_ID, InviteKind.TIMED, valid_for=timedelta(days=7)
    )
    once = await generate.execute(TEST_ADMIN_ID, InviteKind.ONE_TIME)
    limited = await generate.execute(
        TEST_ADMIN_ID, InviteKind.LIMITED, max_uses=25
    )
    assert timed.code == "GOOD2345" and timed.expires_at == timed.created_at + timedelta(days=7)
    assert timed.max_uses is None
    assert once.code == "ONCE2345" and once.max_uses == 1 and once.expires_at is None
    assert limited.max_uses == 25 and limited.expires_at is None
    assert access.create_attempts == 4
    with pytest.raises(ValueError, match="between 2 and 100000"):
        await generate.execute(TEST_ADMIN_ID, InviteKind.LIMITED, max_uses=1)


@pytest.mark.asyncio
async def test_limited_invite_is_consumed_only_after_its_max_uses() -> None:
    access = Access()
    now = Clock().now()
    access.invites["LIMIT234"] = InviteCode(
        "LIMIT234", InviteKind.LIMITED, TEST_ADMIN_ID, now, max_uses=2
    )
    redeem = RedeemInvite(access, Clock())

    assert await redeem.execute(42, "LIMIT234") is InviteRedemption.ACCEPTED
    assert await redeem.execute(43, "LIMIT234") is InviteRedemption.ACCEPTED
    assert await redeem.execute(44, "LIMIT234") is InviteRedemption.USED


@pytest.mark.asyncio
async def test_access_redemption_normalizes_code_and_one_time_is_consumed() -> None:
    access = Access()
    now = Clock().now()
    access.invites["JOIN2345"] = InviteCode(
        "JOIN2345", InviteKind.ONE_TIME, TEST_ADMIN_ID, now, max_uses=1
    )
    redeem = RedeemInvite(access, Clock())
    assert await redeem.execute(42, " join2345 ") is InviteRedemption.ACCEPTED
    assert await CheckAccess(access).execute(42)
    assert await redeem.execute(43, "JOIN2345") is InviteRedemption.USED
    assert await redeem.execute(44, "not-a-code") is InviteRedemption.INVALID


@pytest.mark.asyncio
async def test_only_admin_can_manage_invites() -> None:
    access = Access()
    generate = GenerateInvite(access, Clock(), lambda: "CODE2345")
    with pytest.raises(PermissionError):
        await generate.execute(55, InviteKind.ONE_TIME)
    with pytest.raises(PermissionError):
        await ListInvites(access, Clock()).execute(55)
    with pytest.raises(PermissionError):
        await RevokeInvite(access, Clock()).execute(55, "CODE2345")


@pytest.mark.asyncio
async def test_invite_list_contains_only_currently_usable_codes() -> None:
    access = Access()
    now = Clock().now()
    access.invites = {
        "LIVE2345": InviteCode(
            "LIVE2345", InviteKind.TIMED, TEST_ADMIN_ID, now, now + timedelta(hours=1)
        ),
        "OLDX2345": InviteCode(
            "OLDX2345", InviteKind.TIMED, TEST_ADMIN_ID, now, now
        ),
        "USED2345": InviteCode(
            "USED2345", InviteKind.ONE_TIME, TEST_ADMIN_ID, now, max_uses=1, use_count=1
        ),
    }
    assert [item.code for item in await ListInvites(access, Clock()).execute(TEST_ADMIN_ID)] == [
        "LIVE2345"
    ]


def test_access_tables_are_folded_into_initial_metadata_and_invite_maps() -> None:
    assert {"bot_admins", "invite_codes", "access_grants", "invite_redemptions"} <= set(
        Base.metadata.tables
    )
    now = Clock().now()
    invite = InviteCode("TEST2345", InviteKind.ONE_TIME, TEST_ADMIN_ID, now, max_uses=1)
    values = _invite_values(invite)
    row = type("Row", (), values)()
    assert _invite(row) == invite
