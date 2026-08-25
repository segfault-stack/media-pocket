from __future__ import annotations

from html import escape
from urllib.parse import quote, urlsplit

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from downloader_bot.domain import (
    DeliveryMode,
    ErrorCode,
    InviteKind,
    Job,
    JobStage,
    Platform,
    Progress,
    SelectionMode,
    SelectionRequest,
    UserPreferences,
)

AUDIO_PLATFORMS = frozenset(
    {Platform.SPOTIFY, Platform.SOUNDCLOUD, Platform.HITMOZ, Platform.ZAYCEV}
)
SOCIAL_PLATFORMS = frozenset(
    {
        Platform.TIKTOK,
        Platform.INSTAGRAM,
        Platform.X,
        Platform.THREADS,
        Platform.PINTEREST,
    }
)

START_TEXT = (
    "<b>✨ Download media without the clutter</b>\n\n"
    "Send a link from YouTube, TikTok, Instagram, X, Threads, Pinterest, "
    "SoundCloud, Spotify, HitMoz, or Zaycev. Choose a format, then I’ll deliver it here.\n\n"
    "<b>Quick examples</b>\n"
    "<code>https://youtube.com/watch?v=…</code> — open format picker\n"
    "<code>!a https://youtube.com/watch?v=…</code> — download audio now\n\n"
    "Send several links in one message for a batch. You can also use me inline in any chat."
)
HELP_TEXT = (
    "<b>📖 Downloader Bot help</b>\n\n"
    "<blockquote expandable><b>Platforms</b>\nVideo: YouTube and generic media links.\n"
    "Social: TikTok, Instagram, X, Threads, and Pinterest.\n"
    "Audio: Spotify, SoundCloud, HitMoz, and Zaycev.</blockquote>\n"
    "<blockquote expandable><b>Formats</b>\nUse the picker for Video, Audio, Media, "
    "quality, and Media/File delivery. Use <code>/audio URL</code> or "
    "<code>/video URL</code> to skip confirmation.</blockquote>\n"
    "<blockquote expandable><b>Batch & inline</b>\nPut several links in one message to "
    "apply one choice to all. Type the bot username plus a link to use inline mode.</blockquote>\n"
    "<blockquote expandable><b>Limits</b>\nPrivate, deleted, region-restricted, or "
    "protected media may be unavailable. Telegram file-size limits still apply.</blockquote>"
)
EXPIRED_TEXT = "This request has expired. Send the link again."
ALREADY_HANDLED_TEXT = "This request has already been handled."
NOT_OWNER_TEXT = "Only the person who sent the link can use these controls."
SAVED_TEXT = "Saved"
UNKNOWN_SETTING_TEXT = "Unknown setting"
NO_PERMISSION_TEXT = "You don’t have permission to use this command."
DOWNLOAD_STARTED_TEXT = "Download started"
CANCELLED_TOAST = "Cancelled"
CANCELLATION_REQUESTED_TEXT = "Cancellation requested"
AUDIO_SELECTED_TEXT = "Audio selected"
FILE_SELECTED_TEXT = "File delivery selected"
ALREADY_STARTED_TEXT = "The download has already started"
ACTION_UNAVAILABLE_TEXT = "This action is unavailable"
INLINE_WAITING_TEXT = "⏳ Waiting in queue"
INLINE_TITLE = "⬇️ Download media"
INLINE_OPEN_PRIVATE_TEXT = "Open private chat"
INLINE_SENT_TEXT = "✅ Media sent in private chat"
INLINE_FALLBACK_TEXT = (
    "<b>✨ Your media is ready</b>\nOpen the bot to receive it in private chat."
)
CANCELLED_TEXT = "<b>✖️ Request cancelled</b>"
FAST_AUDIO_HINT = (
    "Add a link after the command, for example:\n"
    "<code>/audio https://example.com/media</code>"
)
FAST_VIDEO_HINT = (
    "Add a link after the command, for example:\n"
    "<code>/video https://example.com/media</code>"
)
AUTO_DELETE_WARNING = (
    "⚠️ I couldn’t delete the source message. Give me permission to delete messages "
    "in this chat, or turn off source cleanup in Settings."
)
MANUAL_RETRY_TEXT = (
    "The previous Telegram upload was interrupted. Try sending it again?"
)
DELIVERED_ITEMS_TEXT = "✅ {count} items delivered"
RESULT_ACTIONS_TEXT = "<b>✅ Download complete</b>"
ACCESS_REQUIRED_TEXT = (
    "<b>🔐 Invite required</b>\n\n"
    "This bot is private. Send your invite code here to unlock downloads."
)
ACCESS_GRANTED_TEXT = (
    "<b>✅ Access unlocked</b>\n\n"
    "You can now send a media link or use the bot inline."
)
INVALID_INVITE_TEXT = (
    "<b>⚠️ This invite is invalid</b>\n\n"
    "Check the code and try again, or ask an administrator for a new invite."
)
EXPIRED_INVITE_TEXT = (
    "<b>⌛ This invite has expired</b>\n\n"
    "Ask an administrator for a new invite."
)
USED_INVITE_TEXT = (
    "<b>🎫 This invite has already been used</b>\n\n"
    "Ask an administrator for a new invite."
)
INLINE_ACCESS_TITLE = "🔐 Invite required"
INLINE_ACCESS_DESCRIPTION = "Open the bot and enter an invite code"
INLINE_ACCESS_MESSAGE = "Open the bot in private chat to unlock downloads."
INLINE_ACCESS_BUTTON = "🔓 Enter invite code"
ADMIN_HOME_TEXT = (
    "<b>🛠 Invite access</b>\n\n"
    "Create a single-use, limited-use, or timed invite code."
)
ADMIN_INVITE_CREATED_TEXT = (
    "<b>🎫 Invite created</b>\n\n"
    "<code>{code}</code>\n"
    "{details}\n\n"
    "Tap the code to copy it."
)
ADMIN_ONE_USE_DETAILS = "One use · no expiry"
ADMIN_LIMITED_DETAILS = "Up to {max_uses} uses · no expiry"
ADMIN_TIMED_DETAILS = "Valid for {duration} · multiple users"
ADMIN_LIMITED_PROMPT_TEXT = (
    "<b>🔢 Limited-use invite</b>\n\n"
    "Send the maximum number of uses as a whole number from 2 to 100000.\n"
    "Send <code>/cancel</code> to stop."
)
ADMIN_LIMITED_INVALID_TEXT = (
    "Enter a whole number from 2 to 100000, or send <code>/cancel</code>."
)
ADMIN_INVITES_EMPTY_TEXT = "<b>🎫 Active invites</b>\n\nNo active invites."
ADMIN_INVITES_TEXT = "<b>🎫 Active invites</b>\n\n{items}"
ADMIN_INVITE_REVOKED_TOAST = "Invite revoked"


def access_required_keyboard(bot_username: str | None) -> InlineKeyboardMarkup | None:
    username = (bot_username or "").lstrip("@")
    if not username:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=INLINE_ACCESS_BUTTON,
                    url=f"https://t.me/{username}?start=invite",
                )
            ]
        ]
    )


def admin_invites_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎫 One use", callback_data="adm:new:once")],
            [
                InlineKeyboardButton(
                    text="🔢 Limited uses", callback_data="adm:new:limited"
                )
            ],
            [
                InlineKeyboardButton(text="⏱ 24 hours", callback_data="adm:new:24h"),
                InlineKeyboardButton(text="📅 7 days", callback_data="adm:new:7d"),
            ],
            [
                InlineKeyboardButton(text="🗓 30 days", callback_data="adm:new:30d"),
                InlineKeyboardButton(text="📋 Active invites", callback_data="adm:list"),
            ],
        ]
    )


def admin_limited_invite_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖ Cancel", callback_data="adm:limited:cancel")]
        ]
    )


def admin_invite_result_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Copy code",
                    copy_text=CopyTextButton(text=code),
                ),
                InlineKeyboardButton(
                    text="➡️ Share invite",
                    url=f"https://t.me/share/url?url={quote(code)}",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="adm:home")],
        ]
    )


def admin_invites_list_keyboard(codes: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"✖ Revoke {code}", callback_data=f"adm:revoke:{code}"
            )
        ]
        for code in codes
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_active_invites(invites) -> str:
    if not invites:
        return ADMIN_INVITES_EMPTY_TEXT
    items: list[str] = []
    for invite in invites:
        if invite.kind is InviteKind.ONE_TIME:
            details = f"one use · {invite.use_count}/1 used"
        elif invite.kind is InviteKind.LIMITED:
            details = f"{invite.use_count}/{invite.max_uses} used · no expiry"
        else:
            details = f"until {invite.expires_at:%Y-%m-%d %H:%M} UTC"
        items.append(f"<code>{escape(invite.code)}</code> · {details}")
    return ADMIN_INVITES_TEXT.format(items="\n".join(items))


def render_stats(values: dict[str, int]) -> str:
    return (
        f"Delivered: {values.get('job_delivered', 0)}\n"
        f"Failed: {values.get('job_failed', 0)}"
    )


def render_admin(values: dict[str, int]) -> str:
    return "<b>Administration</b>\n" + "\n".join(
        f"{key}: {value}" for key, value in sorted(values.items())
    )


STAGE_LABELS = {
    JobStage.QUEUED: "⏳ Waiting in queue",
    JobStage.RESOLVING: "🔎 Checking the link",
    JobStage.DOWNLOADING: "⬇️ Downloading",
    JobStage.PROCESSING: "✨ Preparing media",
    JobStage.READY: "✨ Preparing media",
    JobStage.DELIVERING: "☁️ Sending to Telegram",
    JobStage.DELIVERED: "✅ Delivered",
    JobStage.RETRYING: "🔄 Retrying",
    JobStage.CANCELLING: "✖️ Cancelling",
    JobStage.CANCELLED: "✖️ Cancelled",
    JobStage.FAILED: "⚠️ Download failed",
}
ERROR_CARDS = {
    ErrorCode.PRIVATE: (
        "This media is private.",
        "Open its privacy settings or send a public link.",
    ),
    ErrorCode.DELETED: (
        "This media was deleted.",
        "Check the source and send another link.",
    ),
    ErrorCode.UNSUPPORTED: (
        "This link or format is not supported.",
        "Open Help to see supported platforms and formats.",
    ),
    ErrorCode.REGION_RESTRICTED: (
        "This media is not available in the bot’s region.",
        "Try another public source for the same media.",
    ),
    ErrorCode.RATE_LIMITED: (
        "The platform is temporarily limiting downloads.",
        "Wait a few minutes, then try again.",
    ),
    ErrorCode.EXPIRED: (
        "The source link has expired.",
        "Get a fresh link and send it again.",
    ),
    ErrorCode.TOO_LARGE: (
        "The file exceeds Telegram’s size limit.",
        "Choose a lower quality or audio format.",
    ),
    ErrorCode.UNAVAILABLE: (
        "This media is unavailable.",
        "Check that the link is public and still exists.",
    ),
    ErrorCode.PROVIDER_FAILURE: (
        "The platform could not prepare this media.",
        "Try again shortly or choose another format.",
    ),
    ErrorCode.CANCELLED: (
        "The download was cancelled.",
        "Send the link to start again.",
    ),
}


def start_keyboard(bot_username: str | None = None) -> InlineKeyboardMarkup:
    username = (bot_username or "").lstrip("@")
    rows = [
        [InlineKeyboardButton(text="⚡ Try inline", switch_inline_query="")],
        [
            InlineKeyboardButton(text="⚙️ Settings", callback_data="nav:settings"),
            InlineKeyboardButton(text="📖 Help", callback_data="nav:help"),
        ],
    ]
    if username:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Add to group",
                        url=f"https://t.me/{username}?startgroup=download",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↗️ Share bot",
                        url=f"https://t.me/share/url?url=https://t.me/{username}",
                    )
                ],
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_pending_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Live status", callback_data=f"inline:pending:{token}"
                )
            ]
        ]
    )


def render_selection(selection: SelectionRequest) -> str:
    platforms = ", ".join(
        dict.fromkeys(_platform_name(item) for item in selection.platforms)
    )
    if len(selection.urls) > 1:
        lines = [
            f"<b>⬇️ Download {len(selection.urls)} links</b>",
            escape(platforms),
        ]
    else:
        lines = [f"<b>⬇️ Download from {escape(platforms)}</b>"]
    summary = [_mode_name(selection.mode)]
    if _has_quality(selection):
        summary.append(_quality_name(selection.quality))
    summary.append(_delivery_name(selection.delivery))
    lines.extend(
        [
            " · ".join(summary),
            f"<code>{escape(_short_url(selection.urls[0]))}</code>",
        ]
    )
    if any(
        item in {Platform.INSTAGRAM, Platform.TIKTOK, Platform.X}
        for item in selection.platforms
    ):
        lines.extend(
            ["", "<i>Private, deleted, or restricted posts may be unavailable.</i>"]
        )
    return "\n".join(lines)


def selection_keyboard(selection: SelectionRequest) -> InlineKeyboardMarkup:
    token = selection.token
    rows: list[list[InlineKeyboardButton]] = []
    if all(item in AUDIO_PLATFORMS for item in selection.platforms):
        rows.append([_choice("🎧 Audio", "mode", "audio", token, True)])
    elif any(item in SOCIAL_PLATFORMS for item in selection.platforms):
        rows.append(
            [
                _choice(
                    "▶️ Media",
                    "mode",
                    "media",
                    token,
                    selection.mode is SelectionMode.MEDIA,
                ),
                _choice(
                    "🎧 Audio",
                    "mode",
                    "audio",
                    token,
                    selection.mode is SelectionMode.AUDIO,
                ),
            ]
        )
    else:
        rows.append(
            [
                _choice(
                    "🎬 Video",
                    "mode",
                    "video",
                    token,
                    selection.mode is SelectionMode.VIDEO,
                ),
                _choice(
                    "🎧 Audio",
                    "mode",
                    "audio",
                    token,
                    selection.mode is SelectionMode.AUDIO,
                ),
            ]
        )
        if _has_quality(selection):
            rows.append(
                [
                    _choice(
                        _quality_name(value),
                        "quality",
                        value,
                        token,
                        selection.quality == value,
                    )
                    for value in ("best", "1080", "720", "480")
                ]
            )
    if all(item in AUDIO_PLATFORMS for item in selection.platforms):
        rows.append([_choice("📄 As file", "delivery", "file", token, True)])
    else:
        rows.append(
            [
                _choice(
                    "▶️ In chat",
                    "delivery",
                    "media",
                    token,
                    selection.delivery is DeliveryMode.MEDIA,
                ),
                _choice(
                    "📄 As file",
                    "delivery",
                    "file",
                    token,
                    selection.delivery is DeliveryMode.FILE,
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬇️ Download", callback_data=f"sel:confirm:{token}"
            ),
            InlineKeyboardButton(text="Cancel", callback_data=f"sel:cancel:{token}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_progress(progress: Progress, *, compact: bool = False) -> str:
    if progress.stage is JobStage.FAILED:
        reason, action = ERROR_CARDS.get(
            progress.error_code or ErrorCode.PROVIDER_FAILURE,
            ERROR_CARDS[ErrorCode.PROVIDER_FAILURE],
        )
        return (
            f"<b>{STAGE_LABELS[progress.stage]}</b>\n{reason}\n\n<b>Next:</b> {action}"
        )
    title = STAGE_LABELS[progress.stage]
    if progress.stage is JobStage.QUEUED and progress.queue_position is not None:
        title += f" · #{progress.queue_position}"
    if progress.stage is JobStage.RETRYING:
        title += f" · {progress.attempt}/{progress.attempt_limit}"
    if progress.stage is JobStage.DOWNLOADING:
        title += f" {progress.percent}%"
    if progress.item_count > 1:
        title += f" · {max(1, progress.item)} of {progress.item_count}"
    if compact or progress.stage not in {JobStage.DOWNLOADING, JobStage.PROCESSING}:
        return f"<b>{title}</b>"
    lines = [f"<b>{title}</b>", _progress_bar(progress.percent)]
    if metrics := _progress_metrics(progress):
        lines.append(metrics)
    return "\n".join(lines)


def failure_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Try again", callback_data=f"result:retry:{job_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Change format", callback_data=f"result:format:{job_id}"
                )
            ],
            [InlineKeyboardButton(text="❓ Help", callback_data="nav:help")],
        ]
    )


def render_status(jobs: tuple[Job, ...]) -> str:
    if not jobs:
        return "<b>📊 Your downloads</b>\nNo recent downloads yet."
    lines = ["<b>📊 Your downloads</b>"]
    lines.extend(
        (
            f"\n{STAGE_LABELS[job.stage]} · <code>{escape(_short_url(job.source_url))}</code>"
        )
        for job in jobs
    )
    return "".join(lines)


def status_keyboard(jobs: tuple[Job, ...]) -> InlineKeyboardMarkup | None:
    rows = []
    for job in jobs:
        if not job.terminal:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✖️ Cancel {_short_url(job.source_url, 22)}",
                        callback_data=f"job:cancel:{job.id}",
                    )
                ]
            )
        elif job.stage is JobStage.FAILED:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🔄 Retry {_short_url(job.source_url, 22)}",
                        callback_data=f"result:retry:{job.id}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def settings_home_text(value: UserPreferences) -> str:
    return (
        "<b>⚙️ Settings</b>\nChoose a category. Changes are saved immediately.\n\n"
        f"YouTube: {_youtube_mode_name(value.youtube_mode)} · "
        f"{_quality_name(value.quality)} · {'File' if value.document_mode else 'Media'}"
    )


def settings_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Download & Quality", callback_data="settings:page:download"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Delivery", callback_data="settings:page:delivery"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Chat & Cleanup", callback_data="settings:page:chat"
                )
            ],
        ]
    )


def settings_page(
    value: UserPreferences, page: str
) -> tuple[str, InlineKeyboardMarkup]:
    if page == "download":
        text = (
            "<b>🎬 Download & Quality</b>\n"
            "YouTube links start immediately unless Always ask is selected. "
            "Audio-first services keep their natural audio mode.\n\n"
            f"YouTube: {_youtube_mode_name(value.youtube_mode)}\n"
            f"Video quality: {_quality_name(value.quality)}"
        )
        rows = [
            [
                _youtube_setting("🎬 Video", "video", value.youtube_mode == "video"),
                _youtube_setting("🎧 Audio", "audio", value.youtube_mode == "audio"),
            ],
            [_youtube_setting("💬 Always ask", "ask", value.youtube_mode == "ask")],
            [
                _quality_setting(item, value.quality == item)
                for item in ("best", "1080", "720", "480")
            ],
        ]
    elif page == "delivery":
        text = f"<b>📦 Delivery</b>\nChoose how completed downloads are sent.\n\nDelivery: {'File' if value.document_mode else 'Media'}\nResult actions: {_on_off(value.show_buttons)}"
        rows = [
            [
                _setting("▶️ Media", "document_mode", not value.document_mode),
                _setting("📄 File", "document_mode", value.document_mode),
            ],
            [_toggle_setting("Result actions", "show_buttons", value.show_buttons)],
        ]
    else:
        text = f"<b>💬 Chat & Cleanup</b>\nControl source cleanup and status detail.\n\nDelete source message: {_on_off(value.delete_source)}\nCompact progress: {_on_off(value.compact_progress)}"
        rows = [
            [_toggle_setting("Delete source", "delete_source", value.delete_source)],
            [
                _toggle_setting(
                    "Compact progress", "compact_progress", value.compact_progress
                )
            ],
        ]
    rows.append([InlineKeyboardButton(text="‹ Back", callback_data="settings:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _choice(
    label: str, action: str, value: str, token: str, selected: bool
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=("✓ " if selected else "") + label,
        callback_data=f"sel:{action}:{value}:{token}",
    )


def _setting(label: str, field: str, selected: bool) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=("✓ " if selected else "") + label,
        callback_data=f"settings:set:{field}:{str(not selected).lower()}",
    )


def _toggle_setting(label: str, field: str, selected: bool) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=f"{label}: {_on_off(selected)}", callback_data=f"settings:toggle:{field}"
    )


def _quality_setting(value: str, selected: bool) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=("✓ " if selected else "") + _quality_name(value),
        callback_data=f"settings:quality:{value}",
    )


def _youtube_setting(label: str, value: str, selected: bool) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=("✓ " if selected else "") + label,
        callback_data=f"settings:youtube:{value}",
    )


def _youtube_mode_name(value: str) -> str:
    return {"video": "Video", "audio": "Audio", "ask": "Always ask"}.get(
        value, "Video"
    )


def _has_quality(selection: SelectionRequest) -> bool:
    return selection.mode is SelectionMode.VIDEO and not any(
        item in SOCIAL_PLATFORMS | AUDIO_PLATFORMS for item in selection.platforms
    )


def _platform_name(platform: Platform) -> str:
    return {Platform.X: "X", Platform.HITMOZ: "HitMoz", Platform.ZAYCEV: "Zaycev"}.get(
        platform, platform.value.title()
    )


def _mode_name(mode: SelectionMode) -> str:
    return {
        SelectionMode.VIDEO: "🎬 Video",
        SelectionMode.AUDIO: "🎧 Audio",
        SelectionMode.MEDIA: "▶️ Media",
    }[mode]


def _delivery_name(delivery: DeliveryMode) -> str:
    return "📄 As file" if delivery is DeliveryMode.FILE else "▶️ In chat"


def _quality_name(value: str) -> str:
    return "Best" if value == "best" else f"{value}p"


def _short_url(url: str, limit: int = 52) -> str:
    parsed = urlsplit(url)
    value = f"{parsed.hostname or ''}{parsed.path or ''}"
    if parsed.query:
        value += f"?{parsed.query}"
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _progress_bar(percent: int) -> str:
    filled = max(0, min(10, round(percent / 10)))
    return "▰" * filled + "▱" * (10 - filled)


def _progress_metrics(progress: Progress) -> str:
    parts = []
    if progress.downloaded_bytes is not None:
        size = _bytes(progress.downloaded_bytes)
        if progress.total_bytes:
            size += f" / {_bytes(progress.total_bytes)}"
        parts.append(size)
    if progress.speed_bytes_per_second:
        parts.append(f"{_bytes(progress.speed_bytes_per_second)}/s")
    if progress.eta_seconds is not None:
        parts.append(f"ETA {_duration(progress.eta_seconds)}")
    return " · ".join(parts)


def _bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _duration(seconds: int) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"{minutes}:{remainder:02d}" if minutes else f"{remainder}s"


def _on_off(value: bool) -> str:
    return "On" if value else "Off"
