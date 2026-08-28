from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .audio_names import normalize_artist_names, split_audio_artist_title
from .models import MediaChapter

_TRACK_NUMBER_RE = re.compile(
    r"^\s*(?:(?:track\s*)?#?\d{1,3}\s*(?:[.):]|[-–—])\s*)",
    re.IGNORECASE,
)
_LABELED_ARTIST_RE = re.compile(
    r"^\s*(?:album\s+artist|artists?|music\s+by|исполнитель|музыка)\s*:\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_LABELED_ALBUM_RE = re.compile(
    r"^\s*(?:album|release|альбом|релиз)\s*:\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_MIX_TITLE_RE = re.compile(
    r"^(?P<artists>.+?)\s+(?:mix|mixtape)\b(?P<title>.*)$",
    re.IGNORECASE,
)
_ARTIST_PLAYLIST_RE = re.compile(
    r"^(?P<artist>.+?)\s+(?:playlist|discography)\s*$",
    re.IGNORECASE,
)
_NAMED_SONGS_RE = re.compile(
    r"\b(?:songs?\s+(?:by|of)|music\s+by|песен|треков)\s+(?P<artist>[^#|♡]+)",
    re.IGNORECASE,
)
_MULTI_ARTIST_SEPARATOR_RE = re.compile(r"\s+(?:/|&|\+)\s+|\s*/\s*")
_GENERIC_ARTIST_KEYS = {
    "artist",
    "artists",
    "mix",
    "mixtape",
    "playlist",
    "various",
    "various artists",
    "va",
}


@dataclass(frozen=True, slots=True)
class YouTubeAlbumMetadata:
    title: str
    author: str | None
    asset_author: str | None
    chapters: tuple[MediaChapter, ...]


def resolve_youtube_album_metadata(
    metadata: Mapping[str, Any], chapters: tuple[MediaChapter, ...]
) -> YouTubeAlbumMetadata:
    """Resolve album and chapter names using only explicit, repeatable evidence."""
    video_title = _text(metadata.get("title") or metadata.get("fulltitle")) or "audio"
    description = _text(metadata.get("description"), preserve_lines=True)
    labeled_artist = _first_labeled(description, _LABELED_ARTIST_RE)
    labeled_album = _first_labeled(description, _LABELED_ALBUM_RE)
    provider_artist = (
        metadata.get("artists") or metadata.get("artist") or metadata.get("album_artist")
    )

    cleaned_chapters: list[MediaChapter] = []
    chapter_artists: list[str] = []
    for chapter in chapters:
        label = _TRACK_NUMBER_RE.sub("", chapter.title).strip() or chapter.title.strip()
        split = split_audio_artist_title(label)
        if split:
            chapter_artist, label = split
            normalized_artist = normalize_artist_names(chapter_artist)
        else:
            normalized_artist = None
        if normalized_artist:
            chapter_artists.append(normalized_artist)
        cleaned_chapters.append(
            MediaChapter(label, chapter.start_ms, chapter.end_ms, normalized_artist)
        )

    title_split = split_audio_artist_title(video_title)
    title_artist = title_split[0] if title_split else None
    collection_title = (
        _text(metadata.get("album"))
        or labeled_album
        or (title_split[1] if title_split else video_title)
    )

    artist_candidates = _credible_artists(
        _artist_values(provider_artist or labeled_artist or title_artist)
    )
    mix_match = _MIX_TITLE_RE.match(video_title)
    if not artist_candidates and mix_match:
        artist_candidates = _credible_artists(
            _artist_values(mix_match.group("artists"))
        )
        mix_name = mix_match.group("title").strip(" -–—:|")
        if mix_name:
            collection_title = f"Mix {mix_name}".strip()

    unanimous_chapter_artist = _unanimous(chapter_artists, len(cleaned_chapters))
    if not artist_candidates and unanimous_chapter_artist:
        artist_candidates = (unanimous_chapter_artist,)

    if not artist_candidates:
        playlist_match = _ARTIST_PLAYLIST_RE.match(video_title)
        named_match = _NAMED_SONGS_RE.search(video_title)
        wrapper_artist = (
            playlist_match.group("artist")
            if playlist_match and unanimous_chapter_artist
            else named_match.group("artist")
            if named_match
            else None
        )
        artist_candidates = _credible_artists(_artist_values(wrapper_artist))
    collection_author = normalize_artist_names(artist_candidates)
    asset_author = artist_candidates[0] if len(artist_candidates) == 1 else None

    normalized_chapters = tuple(
        MediaChapter(
            chapter.title,
            chapter.start_ms,
            chapter.end_ms,
            _canonical_artist(chapter.author, artist_candidates) or asset_author,
        )
        for chapter in cleaned_chapters
    )
    return YouTubeAlbumMetadata(
        title=collection_title,
        author=collection_author,
        asset_author=asset_author,
        chapters=normalized_chapters,
    )


def _first_labeled(description: str, pattern: re.Pattern[str]) -> str | None:
    for line in description.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        value = match.group("value").strip()
        if value and "http://" not in value and "https://" not in value:
            return value
    return None


def _artist_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return _artist_values(value.get("name"))
    if isinstance(value, (list, tuple, set, frozenset)):
        values = (artist for item in value for artist in _artist_values(item))
        return tuple(dict.fromkeys(values))
    normalized = normalize_artist_names(value)
    if not normalized:
        return ()
    parts = tuple(
        normalize_artist_names(part)
        for part in _MULTI_ARTIST_SEPARATOR_RE.split(normalized)
        if part.strip()
    )
    return tuple(dict.fromkeys(part for part in parts if part))


def _credible_artists(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        artist for artist in values if _artist_key(artist) not in _GENERIC_ARTIST_KEYS
    )


def _unanimous(values: list[str], chapter_count: int) -> str | None:
    if len(values) != chapter_count or not values:
        return None
    first = values[0]
    return first if all(_artist_key(value) == _artist_key(first) for value in values) else None


def _canonical_artist(value: str | None, candidates: tuple[str, ...]) -> str | None:
    if not value:
        return None
    key = _artist_key(value)
    return next((candidate for candidate in candidates if _artist_key(candidate) == key), value)


def _artist_key(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.casefold())


def _text(value: Any, *, preserve_lines: bool = False) -> str:
    text = str(value or "").strip()
    return text if preserve_lines else re.sub(r"\s+", " ", text)
