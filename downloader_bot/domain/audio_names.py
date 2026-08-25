from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_ARTIST_SEPARATOR_RE = re.compile(r"\s*[,;|·]\s*")
_ARTIST_TOPIC_SUFFIX_RE = re.compile(r"\s*[-–—]\s*topic\s*$", re.IGNORECASE)
_FUZZY_TITLE_PREFIX_RE = re.compile(r"^(?P<prefix>.+?)\s+[-–—:|]\s+(?P<title>.+)$")
_ARTIST_TITLE_SEPARATOR_RE = re.compile(
    r"^(?P<artist>.+?)(?:\s+[-–—]\s*|\s*[-–—]\s+)(?P<title>.+)$"
)
_TITLE_NOISE_SUFFIX_RE = re.compile(
    r"(?:"
    r"\s*[\[(]\s*(?:official\s+(?:music\s+)?(?:video|audio)|"
    r"lyrics?|lyric\s+video|visuali[sz]er|audio\s+only)\s*[\])]"
    r"|\s+[-–—|]\s+(?:official\s+(?:music\s+)?(?:video|audio)|"
    r"lyrics?|lyric\s+video|visuali[sz]er|audio\s+only)"
    r")\s*$",
    re.IGNORECASE,
)
_FUZZY_IGNORED_ARTIST_TOKENS = {"feat", "featuring", "ft", "with", "x"}
_MAX_AUDIO_FILENAME_BYTES = 180


def _normalize_text(value: Any, *, default: str = "") -> str:
    normalized = unicodedata.normalize("NFKC", str(value or default))
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _clean_artist_name(value: Any) -> str:
    normalized = _normalize_text(value)
    normalized = _ARTIST_TOPIC_SUFFIX_RE.sub("", normalized)
    return normalized.strip(" ,;|·")


def _artist_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _fuzzy_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    normalized = _WHITESPACE_RE.sub(
        " ", re.sub(r"[^\w]+", " ", without_marks, flags=re.UNICODE)
    ).strip()
    return " ".join(
        token
        for token in normalized.split()
        if token not in _FUZZY_IGNORED_ARTIST_TOKENS
    )


def _is_near_artist_prefix(prefix: str, artist: str) -> bool:
    prefix_key = _fuzzy_key(prefix)
    artist_key = _fuzzy_key(artist)
    if not prefix_key or not artist_key:
        return False
    if prefix_key == artist_key:
        return True
    if min(len(prefix_key), len(artist_key)) < 5:
        return False
    prefix_tokens = prefix_key.split()
    artist_tokens = artist_key.split()
    if len(prefix_tokens) == len(artist_tokens) and sorted(prefix_tokens) == sorted(
        artist_tokens
    ):
        return True
    direct_ratio = SequenceMatcher(None, prefix_key, artist_key).ratio()
    token_sort_ratio = SequenceMatcher(
        None, " ".join(sorted(prefix_tokens)), " ".join(sorted(artist_tokens))
    ).ratio()
    return max(direct_ratio, token_sort_ratio) >= 0.92


def _strip_title_noise_suffix(title: str) -> str:
    normalized = title
    while True:
        match = _TITLE_NOISE_SUFFIX_RE.search(normalized)
        if not match:
            return normalized
        candidate = normalized[: match.start()].rstrip(" -–—:|")
        if not candidate:
            return normalized
        normalized = candidate


def _deduplicate_repeated_delimited_names(value: str) -> str:
    parts = [_clean_artist_name(part) for part in _ARTIST_SEPARATOR_RE.split(value)]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return value
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = _artist_key(part)
        if key in seen:
            continue
        seen.add(key)
        unique.append(part)
    # A comma can be part of a legitimate single name. Only rebuild the value
    # when the provider actually repeated at least one name.
    if len(unique) == len(parts):
        return value
    return ", ".join(unique)


def _iter_artist_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        yield value.get("name")
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_artist_values(item)
        return
    yield value


def normalize_artist_names(value: Any) -> str | None:
    """Normalize and case-insensitively deduplicate provider artist metadata."""
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in _iter_artist_values(value):
        name = _clean_artist_name(raw_name)
        if not name:
            continue
        name = _deduplicate_repeated_delimited_names(name)
        key = _artist_key(name)
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return ", ".join(names) or None


def split_audio_artist_title(value: Any) -> tuple[str, str] | None:
    """Split a title when a dash has whitespace on at least one side."""
    normalized = _normalize_text(value)
    match = _ARTIST_TITLE_SEPARATOR_RE.match(normalized)
    if not match:
        return None
    artist = _clean_artist_name(match.group("artist"))
    title = _normalize_text(match.group("title"))
    if not artist or not title:
        return None
    return artist, title


def normalize_audio_title(title: Any, artist: Any = None) -> str:
    """Clean provider noise and repeated exact or near-match artist prefixes."""
    normalized_title = _normalize_text(title, default="audio").strip(" -–—:|") or "audio"
    normalized_artist = normalize_artist_names(artist)
    if normalized_artist:
        exact_artist_prefix = re.compile(
            rf"^{re.escape(normalized_artist)}\s*[-–—:|]\s*(?P<title>.+)$",
            re.IGNORECASE,
        )
        while True:
            exact_match = exact_artist_prefix.match(normalized_title)
            if exact_match:
                remaining = exact_match.group("title").strip()
            else:
                fuzzy_match = _FUZZY_TITLE_PREFIX_RE.match(normalized_title)
                if not fuzzy_match or not _is_near_artist_prefix(
                    fuzzy_match.group("prefix"), normalized_artist
                ):
                    break
                remaining = fuzzy_match.group("title").strip()
            if not remaining or remaining == normalized_title:
                break
            normalized_title = remaining
    return _strip_title_noise_suffix(normalized_title).strip() or "audio"


def resolve_audio_title_artist(metadata: Mapping[str, Any]) -> tuple[str, str | None]:
    """Resolve provider metadata using the legacy audio naming precedence."""
    title = _normalize_text(
        metadata.get("track") or metadata.get("title") or metadata.get("fulltitle"),
        default="audio",
    )
    artist = normalize_artist_names(
        metadata.get("artists") or metadata.get("artist") or metadata.get("album_artist")
    )
    if not artist:
        split = split_audio_artist_title(title)
        if split:
            possible_artist, title = split
            artist = normalize_artist_names(possible_artist)
    if not artist:
        artist = normalize_artist_names(
            metadata.get("creator") or metadata.get("uploader") or metadata.get("channel")
        )
    return normalize_audio_title(title, artist), artist


def build_audio_filename(
    title: str | None, artist: str | None = None, *, suffix: str = ".m4a"
) -> str:
    """Return a readable, portable filename within Telegram's practical limits."""
    clean_title = normalize_audio_title(title, artist)
    display_name = f"{artist} - {clean_title}" if artist else clean_title
    display_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " - ", display_name)
    display_name = _WHITESPACE_RE.sub(" ", display_name).strip(" .-") or "audio"
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    max_stem_bytes = _MAX_AUDIO_FILENAME_BYTES - len(normalized_suffix.encode())
    encoded = display_name.encode("utf-8")
    if len(encoded) > max_stem_bytes:
        display_name = encoded[:max_stem_bytes].decode("utf-8", errors="ignore").rstrip()
    return f"{display_name or 'audio'}{normalized_suffix}"
