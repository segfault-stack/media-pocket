from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    exists,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from downloader_bot.domain import (
    DeliveryMode,
    DownloadArtifact,
    ErrorCode,
    InviteCode,
    InviteKind,
    InviteRedemption,
    Job,
    JobKind,
    JobStage,
    MediaKind,
    Platform,
    SelectionMode,
    SelectionRequest,
    UserPreferences,
)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "app_users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AdminRow(Base):
    __tablename__ = "bot_admins"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class InviteRow(Base):
    __tablename__ = "invite_codes"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("bot_admins.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    max_uses: Mapped[int | None] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("use_count >= 0", name="ck_invite_use_count_nonnegative"),
        CheckConstraint(
            "(kind = 'timed' AND expires_at IS NOT NULL AND max_uses IS NULL) OR "
            "(kind = 'one_time' AND expires_at IS NULL AND max_uses = 1) OR "
            "(kind = 'limited' AND expires_at IS NULL "
            "AND max_uses BETWEEN 2 AND 100000)",
            name="ck_invite_policy",
        ),
    )


class AccessGrantRow(Base):
    __tablename__ = "access_grants"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    invite_code: Mapped[str | None] = mapped_column(
        ForeignKey("invite_codes.code", ondelete="SET NULL"), index=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class InviteRedemptionRow(Base):
    __tablename__ = "invite_redemptions"
    invite_code: Mapped[str] = mapped_column(
        ForeignKey("invite_codes.code", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PreferencesRow(Base):
    __tablename__ = "user_preferences"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    quality: Mapped[str] = mapped_column(String(32), default="best")
    audio_format: Mapped[str] = mapped_column(String(16), default="m4a")
    captions: Mapped[bool] = mapped_column(Boolean, default=True)
    document_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    show_buttons: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_source: Mapped[bool] = mapped_column(Boolean, default=True)
    default_audio_only: Mapped[bool] = mapped_column(Boolean, default=False)
    compact_progress: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_mode: Mapped[str] = mapped_column(String(16), default="video")


class JobRow(Base):
    __tablename__ = "download_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE")
    )
    is_parent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    children_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    business_connection_id: Mapped[str | None] = mapped_column(String(128))
    inline_message_id: Mapped[str | None] = mapped_column(String(256))
    audio_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    preferences_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    artifacts_cleaned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    __table_args__ = (
        Index(
            "uq_download_jobs_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("stage NOT IN ('delivered', 'cancelled', 'failed')"),
        ),
        Index("ix_download_jobs_outstanding", "stage", "updated_at"),
    )


class OutboxRow(Base):
    __tablename__ = "job_outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(
        String(64), nullable=False, default="download.requested"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class AnalyticsRow(Base):
    __tablename__ = "app_analytics"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class CacheRow(Base):
    __tablename__ = "media_cache_v2"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    manifest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class SelectionRow(Base):
    __tablename__ = "download_selections"
    token: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    urls_json: Mapped[str] = mapped_column(Text, nullable=False)
    platforms_json: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery: Mapped[str] = mapped_column(String(16), nullable=False)
    job_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    business_connection_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SpotifyCredentialsRow(Base):
    __tablename__ = "spotify_credentials"
    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    credentials: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


def create_engine(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class SqlUserRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def ensure(self, user_id: int) -> None:
        async with self._sessions.begin() as session:
            stmt = (
                pg_insert(UserRow)
                .values(id=user_id)
                .on_conflict_do_nothing(index_elements=[UserRow.id])
            )
            await session.execute(stmt)


class SqlAccessRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add_admin(self, user_id: int, *, added_by: int | None = None) -> bool:
        async with self._sessions.begin() as session:
            await session.execute(
                pg_insert(UserRow)
                .values(id=user_id)
                .on_conflict_do_nothing(index_elements=[UserRow.id])
            )
            inserted = await session.scalar(
                pg_insert(AdminRow)
                .values(user_id=user_id, added_by=added_by)
                .on_conflict_do_nothing(index_elements=[AdminRow.user_id])
                .returning(AdminRow.user_id)
            )
            return inserted is not None

    async def has_admin(self) -> bool:
        async with self._sessions() as session:
            return bool(
                await session.scalar(
                    select(exists().where(AdminRow.user_id.is_not(None)))
                )
            )

    async def is_admin(self, user_id: int) -> bool:
        async with self._sessions() as session:
            return bool(
                await session.scalar(
                    select(exists().where(AdminRow.user_id == user_id))
                )
            )

    async def is_allowed(self, user_id: int) -> bool:
        async with self._sessions() as session:
            return bool(
                await session.scalar(
                    select(
                        or_(
                            exists().where(AdminRow.user_id == user_id),
                            exists().where(AccessGrantRow.user_id == user_id),
                        )
                    )
                )
            )

    async def create_invite(self, invite: InviteCode) -> bool:
        async with self._sessions.begin() as session:
            inserted = await session.scalar(
                pg_insert(InviteRow)
                .values(**_invite_values(invite))
                .on_conflict_do_nothing(index_elements=[InviteRow.code])
                .returning(InviteRow.code)
            )
            return inserted is not None

    async def redeem_invite(
        self, code: str, user_id: int, now: datetime
    ) -> InviteRedemption:
        async with self._sessions.begin() as session:
            if await session.scalar(
                select(
                    or_(
                        exists().where(AdminRow.user_id == user_id),
                        exists().where(AccessGrantRow.user_id == user_id),
                    )
                )
            ):
                return InviteRedemption.ALREADY_AUTHORIZED
            row = await session.scalar(
                select(InviteRow)
                .where(InviteRow.code == code)
                .with_for_update()
            )
            if row is None:
                return InviteRedemption.INVALID
            # Another redemption for this user may have completed while this
            # transaction waited for the invite row lock.
            if await session.scalar(
                select(
                    or_(
                        exists().where(AdminRow.user_id == user_id),
                        exists().where(AccessGrantRow.user_id == user_id),
                    )
                )
            ):
                return InviteRedemption.ALREADY_AUTHORIZED
            if row.revoked_at is not None:
                return InviteRedemption.REVOKED
            if row.expires_at is not None and row.expires_at <= now:
                return InviteRedemption.EXPIRED
            if row.max_uses is not None and row.use_count >= row.max_uses:
                return InviteRedemption.USED
            await session.execute(
                pg_insert(UserRow)
                .values(id=user_id)
                .on_conflict_do_nothing(index_elements=[UserRow.id])
            )
            session.add(
                AccessGrantRow(
                    user_id=user_id, invite_code=row.code, granted_at=now
                )
            )
            session.add(
                InviteRedemptionRow(
                    invite_code=row.code, user_id=user_id, redeemed_at=now
                )
            )
            row.use_count += 1
            return InviteRedemption.ACCEPTED

    async def list_invites(
        self, created_by: int, now: datetime
    ) -> tuple[InviteCode, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(InviteRow)
                .where(
                    InviteRow.created_by == created_by,
                    InviteRow.revoked_at.is_(None),
                    or_(InviteRow.expires_at.is_(None), InviteRow.expires_at > now),
                    or_(
                        InviteRow.max_uses.is_(None),
                        InviteRow.use_count < InviteRow.max_uses,
                    ),
                )
                .order_by(InviteRow.created_at.desc())
                .limit(100)
            )
            return tuple(_invite(row) for row in rows)

    async def revoke_invite(self, code: str, admin_id: int, now: datetime) -> bool:
        async with self._sessions.begin() as session:
            if not await session.scalar(
                select(exists().where(AdminRow.user_id == admin_id))
            ):
                return False
            updated = await session.scalar(
                update(InviteRow)
                .where(InviteRow.code == code, InviteRow.revoked_at.is_(None))
                .values(revoked_at=now)
                .returning(InviteRow.code)
            )
            return updated is not None


class SqlSettingsRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, user_id: int) -> UserPreferences:
        async with self._sessions() as session:
            row = await session.get(PreferencesRow, user_id)
            return _preferences(row) if row else UserPreferences()

    async def save(self, user_id: int, preferences: UserPreferences) -> UserPreferences:
        values = {
            "quality": preferences.quality,
            "audio_format": preferences.audio_format,
            "captions": preferences.captions,
            "document_mode": preferences.document_mode,
            "show_buttons": preferences.show_buttons,
            "delete_source": preferences.delete_source,
            "default_audio_only": preferences.youtube_mode == "audio",
            "compact_progress": preferences.compact_progress,
            "youtube_mode": preferences.youtube_mode,
        }
        values["user_id"] = user_id
        async with self._sessions.begin() as session:
            await session.execute(
                pg_insert(UserRow)
                .values(id=user_id)
                .on_conflict_do_nothing(index_elements=[UserRow.id])
            )
            await session.execute(
                pg_insert(PreferencesRow)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[PreferencesRow.user_id], set_=values
                )
            )
        return preferences


class SqlJobRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_with_outbox(self, job: Job) -> tuple[Job, bool]:
        async with self._sessions.begin() as session:
            inserted = await session.scalar(
                pg_insert(JobRow)
                .values(**_job_values(job))
                .on_conflict_do_nothing(
                    index_elements=[JobRow.dedupe_key],
                    index_where=text(
                        "stage NOT IN ('delivered', 'cancelled', 'failed')"
                    ),
                )
                .returning(JobRow.id)
            )
            if inserted:
                session.add(OutboxRow(job_id=job.id, created_at=job.created_at))
                return job, True
            active = await session.scalar(
                select(JobRow).where(
                    JobRow.dedupe_key == job.dedupe_key,
                    JobRow.stage.not_in(
                        [
                            JobStage.DELIVERED.value,
                            JobStage.CANCELLED.value,
                            JobStage.FAILED.value,
                        ]
                    ),
                )
            )
            if active is None:
                raise RuntimeError("deduplicated job disappeared during transaction")
            return _job(active), False

    async def create_parent(self, job: Job) -> Job:
        async with self._sessions.begin() as session:
            session.add(JobRow(**_job_values(job)))
        return job

    async def create_batch(self, parent: Job, children: tuple[Job, ...]) -> None:
        async with self._sessions.begin() as session:
            session.add(JobRow(**_job_values(parent)))
            await session.flush()
            for child in children:
                session.add(JobRow(**_job_values(child)))
                session.add(OutboxRow(job_id=child.id, created_at=child.created_at))

    async def get(self, job_id: str) -> Job | None:
        async with self._sessions() as session:
            row = await session.get(JobRow, job_id)
            return _job(row) if row else None

    async def children(self, parent_id: str) -> tuple[Job, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(JobRow)
                .where(JobRow.parent_id == parent_id)
                .order_by(JobRow.created_at)
            )
            return tuple(_job(row) for row in rows)

    async def transition(
        self, job_id: str, expected: set[JobStage], stage: JobStage, **changes: object
    ) -> Job | None:
        values: dict[str, object] = {
            "stage": stage.value,
            "updated_at": datetime.now(UTC),
        }
        values.update(
            {
                key: value.value if isinstance(value, (JobStage, ErrorCode)) else value
                for key, value in changes.items()
            }
        )
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.stage.in_([item.value for item in expected]),
                )
                .values(**values)
                .returning(JobRow)
            )
            row = result.scalar_one_or_none()
            return _job(row) if row else None

    async def request_cancel(self, job_id: str, user_id: int) -> Job | None:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.user_id == user_id,
                    JobRow.stage.not_in(
                        [
                            item.value
                            for item in {
                                JobStage.DELIVERED,
                                JobStage.CANCELLED,
                                JobStage.FAILED,
                            }
                        ]
                    ),
                )
                .values(cancel_requested=True, updated_at=datetime.now(UTC))
                .returning(JobRow)
            )
            row = result.scalar_one_or_none()
            return _job(row) if row else None

    async def request_cancel_children(self, parent_id: str, user_id: int) -> int:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            queued = await session.execute(
                update(JobRow)
                .where(
                    JobRow.parent_id == parent_id,
                    JobRow.user_id == user_id,
                    JobRow.stage == JobStage.QUEUED.value,
                )
                .values(
                    stage=JobStage.CANCELLED.value,
                    cancel_requested=True,
                    error_code=ErrorCode.CANCELLED.value,
                    updated_at=now,
                )
            )
            running = await session.execute(
                update(JobRow)
                .where(
                    JobRow.parent_id == parent_id,
                    JobRow.user_id == user_id,
                    JobRow.stage.not_in(
                        [
                            JobStage.DELIVERED.value,
                            JobStage.CANCELLED.value,
                            JobStage.FAILED.value,
                        ]
                    ),
                )
                .values(cancel_requested=True, updated_at=now)
            )
            return int(getattr(queued, "rowcount", 0) or 0) + int(
                getattr(running, "rowcount", 0) or 0
            )

    async def bind_inline(
        self, job_id: str, user_id: int, inline_message_id: str
    ) -> Job | None:
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    update(JobRow)
                    .where(
                        JobRow.id == job_id,
                        JobRow.user_id == user_id,
                        JobRow.kind == JobKind.INLINE.value,
                    )
                    .values(
                        inline_message_id=inline_message_id,
                        updated_at=datetime.now(UTC),
                    )
                    .returning(JobRow)
                )
            ).scalar_one_or_none()
            return _job(row) if row else None

    async def route_inline_to_private(self, job_id: str, user_id: int) -> Job | None:
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    update(JobRow)
                    .where(JobRow.id == job_id, JobRow.user_id == user_id)
                    .values(inline_message_id=None, updated_at=datetime.now(UTC))
                    .returning(JobRow)
                )
            ).scalar_one_or_none()
            return _job(row) if row else None

    async def customize(
        self,
        job_id: str,
        user_id: int,
        *,
        audio_only: bool | None = None,
        document_mode: bool | None = None,
    ) -> Job | None:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.user_id == user_id,
                    JobRow.stage == JobStage.QUEUED.value,
                )
                .with_for_update()
            )
            if row is None:
                return None
            preferences = _decode_preferences(row.preferences_json)
            if document_mode is not None:
                preferences = replace(preferences, document_mode=document_mode)
            if audio_only is not None:
                row.audio_only = audio_only
            row.preferences_json = json.dumps(asdict(preferences), sort_keys=True)
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return _job(row)

    async def claim_outbox(self, limit: int = 100) -> tuple[tuple[int, str], ...]:
        async with self._sessions.begin() as session:
            rows = (
                (
                    await session.execute(
                        select(OutboxRow)
                        .where(OutboxRow.published_at.is_(None))
                        .order_by(OutboxRow.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            return tuple((row.id, row.job_id) for row in rows)

    async def mark_outbox_published(self, event_id: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == event_id)
                .values(published_at=datetime.now(UTC))
            )

    async def outstanding(self, limit: int = 100) -> tuple[str, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(JobRow.id)
                .where(
                    JobRow.stage.in_([JobStage.QUEUED.value, JobStage.RETRYING.value])
                )
                .order_by(JobRow.updated_at)
                .limit(limit)
            )
            return tuple(rows)

    async def queue_position(self, job_id: str) -> int | None:
        from sqlalchemy import func

        async with self._sessions() as session:
            job = await session.get(JobRow, job_id)
            if job is None or job.is_parent or job.stage != JobStage.QUEUED.value:
                return None
            value = await session.scalar(
                select(func.count())
                .select_from(JobRow)
                .where(
                    JobRow.stage == JobStage.QUEUED.value,
                    JobRow.is_parent.is_(False),
                    JobRow.created_at <= job.created_at,
                )
            )
            return int(value or 1)

    async def expired_artifacts(
        self, before: datetime, limit: int = 100
    ) -> tuple[str, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(JobRow.id)
                .where(
                    JobRow.stage.in_(
                        [
                            JobStage.DELIVERED.value,
                            JobStage.CANCELLED.value,
                            JobStage.FAILED.value,
                        ]
                    ),
                    JobRow.updated_at < before,
                    JobRow.artifacts_cleaned_at.is_(None),
                    JobRow.is_parent.is_(False),
                )
                .order_by(JobRow.updated_at)
                .limit(limit)
            )
            return tuple(rows)

    async def mark_artifacts_cleaned(self, job_id: str, at: datetime) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(JobRow)
                .where(JobRow.id == job_id)
                .values(artifacts_cleaned_at=at)
            )

    async def recent_for_user(
        self, user_id: int, *, limit: int = 10
    ) -> tuple[Job, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(JobRow)
                .where(JobRow.user_id == user_id, JobRow.is_parent.is_(False))
                .order_by(JobRow.updated_at.desc())
                .limit(limit)
            )
            return tuple(_job(row) for row in rows)


class SqlSelectionRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, selection: SelectionRequest) -> SelectionRequest:
        async with self._sessions.begin() as session:
            session.add(SelectionRow(**_selection_values(selection)))
        return selection

    async def get(self, token: str) -> SelectionRequest | None:
        async with self._sessions() as session:
            row = await session.get(SelectionRow, token)
            return _selection(row) if row else None

    async def update(
        self, token: str, user_id: int, now: datetime, **changes: object
    ) -> SelectionRequest | None:
        values = {
            key: value.value
            if isinstance(value, (SelectionMode, DeliveryMode))
            else value
            for key, value in changes.items()
        }
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    update(SelectionRow)
                    .where(
                        SelectionRow.token == token,
                        SelectionRow.user_id == user_id,
                        SelectionRow.claimed_at.is_(None),
                        SelectionRow.cancelled_at.is_(None),
                        SelectionRow.expires_at > now,
                    )
                    .values(**values)
                    .returning(SelectionRow)
                )
            ).scalar_one_or_none()
            return _selection(row) if row else None

    async def claim(
        self, token: str, user_id: int, now: datetime
    ) -> SelectionRequest | None:
        return await self.update(token, user_id, now, claimed_at=now)

    async def release_claim(
        self, token: str, user_id: int, claimed_at: datetime
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(SelectionRow)
                .where(
                    SelectionRow.token == token,
                    SelectionRow.user_id == user_id,
                    SelectionRow.claimed_at == claimed_at,
                    SelectionRow.cancelled_at.is_(None),
                )
                .values(claimed_at=None)
            )

    async def cancel(
        self, token: str, user_id: int, now: datetime
    ) -> SelectionRequest | None:
        return await self.update(token, user_id, now, cancelled_at=now)


class SqlMediaCacheRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, key: str) -> tuple[DownloadArtifact, ...] | None:
        async with self._sessions() as session:
            row = await session.get(CacheRow, key)
            if row is None:
                return None
            artifacts = _decode_artifacts(row.manifest)
            if not artifacts or any(
                not Path(item.path).is_file() for item in artifacts
            ):
                await session.delete(row)
                await session.commit()
                return None
            return artifacts

    async def put(self, key: str, artifacts: tuple[DownloadArtifact, ...]) -> None:
        manifest = json.dumps(
            [
                {
                    "path": str(item.path),
                    "kind": item.kind.value,
                    "size": item.size,
                    "checksum": item.checksum,
                    "mime_type": item.mime_type,
                    "title": item.title,
                    "author": item.author,
                    "duration_ms": item.duration_ms,
                    "thumbnail_path": str(item.thumbnail_path)
                    if item.thumbnail_path
                    else None,
                }
                for item in artifacts
            ]
        )
        async with self._sessions.begin() as session:
            await session.execute(
                pg_insert(CacheRow)
                .values(key=key, manifest=manifest)
                .on_conflict_do_update(
                    index_elements=[CacheRow.key], set_={"manifest": manifest}
                )
            )


class SqlAnalyticsRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self, event: str, *, user_id: int | None = None, job_id: str | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            session.add(AnalyticsRow(event=event, user_id=user_id, job_id=job_id))

    async def stats(self) -> dict[str, int]:
        from sqlalchemy import func

        async with self._sessions() as session:
            rows = await session.execute(
                select(AnalyticsRow.event, func.count()).group_by(AnalyticsRow.event)
            )
            return dict(rows.tuples())


def _job_values(job: Job) -> dict[str, object]:
    return {
        "id": job.id,
        "user_id": job.user_id,
        "chat_id": job.chat_id,
        "source_url": job.source_url,
        "dedupe_key": job.dedupe_key,
        "kind": job.kind.value,
        "stage": job.stage.value,
        "attempt": job.attempt,
        "parent_id": job.parent_id,
        "is_parent": job.is_parent,
        "children_total": job.children_total,
        "status_message_id": job.status_message_id,
        "source_message_id": job.source_message_id,
        "business_connection_id": job.business_connection_id,
        "inline_message_id": job.inline_message_id,
        "audio_only": job.audio_only,
        "preferences_json": json.dumps(asdict(job.preferences), sort_keys=True),
        "cancel_requested": job.cancel_requested,
        "error_code": job.error_code.value if job.error_code else None,
        "error_detail": job.error_detail,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "artifacts_cleaned_at": job.artifacts_cleaned_at,
    }


def _job(row: JobRow) -> Job:
    return Job(
        id=row.id,
        user_id=row.user_id,
        chat_id=row.chat_id,
        source_url=row.source_url,
        dedupe_key=row.dedupe_key,
        kind=JobKind(row.kind),
        stage=JobStage(row.stage),
        attempt=row.attempt,
        parent_id=row.parent_id,
        is_parent=row.is_parent,
        children_total=row.children_total,
        status_message_id=row.status_message_id,
        source_message_id=row.source_message_id,
        business_connection_id=row.business_connection_id,
        inline_message_id=row.inline_message_id,
        audio_only=row.audio_only,
        preferences=_decode_preferences(row.preferences_json),
        cancel_requested=row.cancel_requested,
        error_code=ErrorCode(row.error_code) if row.error_code else None,
        error_detail=row.error_detail,
        created_at=row.created_at,
        updated_at=row.updated_at,
        artifacts_cleaned_at=row.artifacts_cleaned_at,
    )


def _preferences(row: PreferencesRow) -> UserPreferences:
    youtube_mode = row.youtube_mode or (
        "audio" if row.default_audio_only else "video"
    )
    return UserPreferences(
        quality=row.quality,
        audio_format=row.audio_format,
        captions=row.captions,
        document_mode=row.document_mode,
        show_buttons=row.show_buttons,
        delete_source=row.delete_source,
        default_audio_only=row.default_audio_only,
        compact_progress=row.compact_progress,
        youtube_mode=youtube_mode,
    )


def _decode_preferences(value: str) -> UserPreferences:
    data = json.loads(value or "{}")
    allowed = UserPreferences.__dataclass_fields__
    return UserPreferences(
        **{key: item for key, item in data.items() if key in allowed}
    )


def _decode_artifacts(value: str) -> tuple[DownloadArtifact, ...]:
    return tuple(
        DownloadArtifact(
            path=PurePosixPath(item["path"]),
            kind=MediaKind(item["kind"]),
            size=item["size"],
            checksum=item["checksum"],
            mime_type=item.get("mime_type"),
            title=item.get("title"),
            author=item.get("author"),
            duration_ms=item.get("duration_ms"),
            thumbnail_path=PurePosixPath(item["thumbnail_path"])
            if item.get("thumbnail_path")
            else None,
        )
        for item in json.loads(value)
    )


def _selection_values(selection: SelectionRequest) -> dict[str, object]:
    return {
        "token": selection.token,
        "user_id": selection.user_id,
        "chat_id": selection.chat_id,
        "urls_json": json.dumps(selection.urls),
        "platforms_json": json.dumps([item.value for item in selection.platforms]),
        "mode": selection.mode.value,
        "quality": selection.quality,
        "delivery": selection.delivery.value,
        "job_kind": selection.job_kind.value,
        "source_message_id": selection.source_message_id,
        "status_message_id": selection.status_message_id,
        "business_connection_id": selection.business_connection_id,
        "created_at": selection.created_at,
        "expires_at": selection.expires_at,
        "claimed_at": selection.claimed_at,
        "cancelled_at": selection.cancelled_at,
    }


def _selection(row: SelectionRow) -> SelectionRequest:
    return SelectionRequest(
        token=row.token,
        user_id=row.user_id,
        chat_id=row.chat_id,
        urls=tuple(json.loads(row.urls_json)),
        platforms=tuple(Platform(item) for item in json.loads(row.platforms_json)),
        mode=SelectionMode(row.mode),
        quality=row.quality,
        delivery=DeliveryMode(row.delivery),
        job_kind=JobKind(row.job_kind),
        source_message_id=row.source_message_id,
        status_message_id=row.status_message_id,
        business_connection_id=row.business_connection_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        claimed_at=row.claimed_at,
        cancelled_at=row.cancelled_at,
    )


def _invite_values(invite: InviteCode) -> dict[str, object]:
    return {
        "code": invite.code,
        "kind": invite.kind.value,
        "created_by": invite.created_by,
        "created_at": invite.created_at,
        "expires_at": invite.expires_at,
        "max_uses": invite.max_uses,
        "use_count": invite.use_count,
        "revoked_at": invite.revoked_at,
    }


def _invite(row: InviteRow) -> InviteCode:
    return InviteCode(
        code=row.code,
        kind=InviteKind(row.kind),
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        max_uses=row.max_uses,
        use_count=row.use_count,
        revoked_at=row.revoked_at,
    )
