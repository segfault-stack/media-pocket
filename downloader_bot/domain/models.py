from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType


class Platform(StrEnum):
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    X = "x"
    PINTEREST = "pinterest"
    THREADS = "threads"
    SOUNDCLOUD = "soundcloud"
    SPOTIFY = "spotify"
    HITMOZ = "hitmoz"
    ZAYCEV = "zaycev"
    GENERIC = "generic"


class MediaKind(StrEnum):
    VIDEO = "video"
    PHOTO = "photo"
    AUDIO = "audio"
    DOCUMENT = "document"


class MediaSource(StrEnum):
    HTTP = "http"
    SPOTIFY_STREAM = "spotify_stream"


class JobKind(StrEnum):
    DIRECT = "direct"
    BUSINESS = "business"
    GROUP_REPLAY = "group_replay"
    INLINE = "inline"
    BATCH = "batch"
    ALBUM = "album"
    PLAYLIST = "playlist"
    PROFILE = "profile"


class JobStage(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    READY = "ready"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    RETRYING = "retrying"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ErrorCode(StrEnum):
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    PRIVATE = "private"
    TOO_LARGE = "too_large"
    RATE_LIMITED = "rate_limited"
    PROVIDER_FAILURE = "provider_failure"
    CANCELLED = "cancelled"
    DELETED = "deleted"
    REGION_RESTRICTED = "region_restricted"
    EXPIRED = "expired"


class SelectionMode(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    MEDIA = "media"


class DeliveryMode(StrEnum):
    MEDIA = "media"
    FILE = "file"


class InviteKind(StrEnum):
    TIMED = "timed"
    ONE_TIME = "one_time"
    LIMITED = "limited"


class InviteRedemption(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_AUTHORIZED = "already_authorized"
    INVALID = "invalid"
    EXPIRED = "expired"
    USED = "used"
    REVOKED = "revoked"


TERMINAL_STAGES = frozenset({JobStage.DELIVERED, JobStage.CANCELLED, JobStage.FAILED})
ALLOWED_TRANSITIONS: Mapping[JobStage, frozenset[JobStage]] = MappingProxyType(
    {
        JobStage.QUEUED: frozenset(
            {JobStage.RESOLVING, JobStage.CANCELLING, JobStage.FAILED}
        ),
        JobStage.RESOLVING: frozenset(
            {
                JobStage.DOWNLOADING,
                JobStage.RETRYING,
                JobStage.CANCELLING,
                JobStage.FAILED,
            }
        ),
        JobStage.DOWNLOADING: frozenset(
            {
                JobStage.PROCESSING,
                JobStage.RETRYING,
                JobStage.CANCELLING,
                JobStage.FAILED,
            }
        ),
        JobStage.PROCESSING: frozenset(
            {JobStage.READY, JobStage.RETRYING, JobStage.CANCELLING, JobStage.FAILED}
        ),
        JobStage.READY: frozenset(
            {JobStage.DELIVERING, JobStage.CANCELLING, JobStage.FAILED}
        ),
        JobStage.DELIVERING: frozenset({JobStage.DELIVERED, JobStage.FAILED}),
        JobStage.RETRYING: frozenset(
            {
                JobStage.RESOLVING,
                JobStage.DOWNLOADING,
                JobStage.CANCELLING,
                JobStage.FAILED,
            }
        ),
        JobStage.CANCELLING: frozenset({JobStage.CANCELLED}),
        JobStage.DELIVERED: frozenset(),
        JobStage.CANCELLED: frozenset(),
        JobStage.FAILED: frozenset(),
    }
)


@dataclass(frozen=True, slots=True)
class MediaAsset:
    source_url: str
    kind: MediaKind
    index: int = 0
    title: str | None = None
    author: str | None = None
    duration_ms: int | None = None
    size_hint: int | None = None
    source: MediaSource = MediaSource.HTTP
    fallback_query: str | None = None
    request_headers: tuple[tuple[str, str], ...] = ()
    extractor_url: str | None = None
    format_selector: str | None = None
    cookies_file: str | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True, slots=True)
class MediaPost:
    source_url: str
    platform: Platform
    assets: tuple[MediaAsset, ...]
    title: str | None = None
    author: str | None = None
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    path: PurePosixPath
    kind: MediaKind
    size: int
    checksum: str
    mime_type: str | None = None
    title: str | None = None
    author: str | None = None
    duration_ms: int | None = None
    thumbnail_path: PurePosixPath | None = None


@dataclass(frozen=True, slots=True)
class UserPreferences:
    quality: str = "best"
    audio_format: str = "m4a"
    captions: bool = True
    document_mode: bool = False
    show_buttons: bool = True
    delete_source: bool = True
    default_audio_only: bool = False
    compact_progress: bool = False
    youtube_mode: str = "video"

    def cache_variant(self, *, audio_only: bool = False) -> str:
        return ":".join(
            (
                "audio" if audio_only else "media",
                self.quality,
                "m4a" if audio_only else "mp4",
            )
        )


@dataclass(frozen=True, slots=True)
class Progress:
    job_id: str
    stage: JobStage
    percent: int = 0
    attempt: int = 1
    attempt_limit: int = 3
    item: int = 1
    item_count: int = 1
    queue_position: int | None = None
    detail: str | None = None
    error_code: ErrorCode | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bytes_per_second: int | None = None
    eta_seconds: int | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not 0 <= self.percent <= 100:
            raise ValueError("percent must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    user_id: int
    chat_id: int
    source_url: str
    dedupe_key: str
    kind: JobKind = JobKind.DIRECT
    stage: JobStage = JobStage.QUEUED
    attempt: int = 1
    parent_id: str | None = None
    is_parent: bool = False
    children_total: int = 0
    status_message_id: int | None = None
    source_message_id: int | None = None
    business_connection_id: str | None = None
    inline_message_id: str | None = None
    audio_only: bool = False
    preferences: UserPreferences = field(default_factory=UserPreferences)
    cancel_requested: bool = False
    error_code: ErrorCode | None = None
    error_detail: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    artifacts_cleaned_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES

    @property
    def cache_key(self) -> str:
        source_key = hashlib.sha256(self.source_url.encode()).hexdigest()
        return (
            f"{source_key}:{self.preferences.cache_variant(audio_only=self.audio_only)}"
        )

    def transition(self, stage: JobStage, *, now: datetime | None = None) -> Job:
        if stage not in ALLOWED_TRANSITIONS[self.stage]:
            raise InvalidJobTransition(f"{self.stage} -> {stage}")
        return replace(self, stage=stage, updated_at=now or datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class SelectionRequest:
    token: str
    user_id: int
    chat_id: int
    urls: tuple[str, ...]
    platforms: tuple[Platform, ...]
    mode: SelectionMode
    quality: str
    delivery: DeliveryMode
    created_at: datetime
    expires_at: datetime
    job_kind: JobKind = JobKind.DIRECT
    source_message_id: int | None = None
    status_message_id: int | None = None
    business_connection_id: str | None = None
    claimed_at: datetime | None = None
    cancelled_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.claimed_at is None and self.cancelled_at is None


@dataclass(frozen=True, slots=True)
class InviteCode:
    code: str
    kind: InviteKind
    created_by: int
    created_at: datetime
    expires_at: datetime | None = None
    max_uses: int | None = None
    use_count: int = 0
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.code.isascii() or not self.code.isalnum():
            raise ValueError("invite code must be ASCII letters and numbers")
        if self.kind is InviteKind.TIMED:
            if self.expires_at is None or self.max_uses is not None:
                raise ValueError("timed invites require expiry and have no use limit")
        elif self.kind is InviteKind.ONE_TIME and (
            self.expires_at is not None or self.max_uses != 1
        ):
            raise ValueError("one-time invites require max_uses=1 and no expiry")
        elif self.kind is InviteKind.LIMITED and (
            self.expires_at is not None
            or self.max_uses is None
            or not 2 <= self.max_uses <= 100_000
        ):
            raise ValueError(
                "limited invites require max_uses between 2 and 100000 and no expiry"
            )

    def available_at(self, now: datetime) -> bool:
        return (
            self.revoked_at is None
            and (self.expires_at is None or self.expires_at > now)
            and (self.max_uses is None or self.use_count < self.max_uses)
        )


class InvalidJobTransition(ValueError):
    """Raised when a job state transition violates the domain state machine."""
