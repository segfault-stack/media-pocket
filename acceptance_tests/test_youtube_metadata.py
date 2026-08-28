from __future__ import annotations

import pytest

from downloader_bot.domain import MediaChapter
from downloader_bot.domain.youtube_metadata import resolve_youtube_album_metadata


def chapters(*titles: str) -> tuple[MediaChapter, ...]:
    return tuple(
        MediaChapter(title, index * 1_000, (index + 1) * 1_000)
        for index, title in enumerate(titles)
    )


@pytest.mark.parametrize(
    ("metadata", "raw_chapters", "expected_title", "expected_author"),
    [
        (
            {
                "title": "The Gerogerigegege - Sexual Behavior In The Human Male (1995 SSE Communications CD)",
                "uploader": "BrokenMindMusic",
            },
            chapters("01: Water Business", "02: Sexual Behavior In The Human Male"),
            "Sexual Behavior In The Human Male (1995 SSE Communications CD)",
            "The Gerogerigegege",
        ),
        (
            {
                "title": "Oxxxymiron - miXXXtape I (mixed by OFFbeat) (2008-2012)",
                "uploader": "Hip-Hop Tracks news",
            },
            chapters("0: Intro", "1: Йети и дети (Куплет 1)"),
            "miXXXtape I (mixed by OFFbeat) (2008-2012)",
            "Oxxxymiron",
        ),
        (
            {"title": "Crystal Castles Playlist", "uploader": "abaddon"},
            chapters(
                "Crystal Castles - Vanished",
                "Crystal Castles - Kerosene",
            ),
            "Crystal Castles Playlist",
            "Crystal Castles",
        ),
        (
            {
                "title": "♡ плейлист старых песен Пошлой Молли #2 ♡",
                "uploader": "CHERRY MSX",
            },
            chapters("01: Нон стоп", "02: Буду твоим пёсиком"),
            "♡ плейлист старых песен Пошлой Молли #2 ♡",
            "Пошлой Молли",
        ),
    ],
)
def test_single_artist_album_formats_produce_track_metadata(
    metadata, raw_chapters, expected_title, expected_author
) -> None:
    result = resolve_youtube_album_metadata(metadata, raw_chapters)

    assert result.title == expected_title
    assert result.author == result.asset_author == expected_author
    assert all(chapter.author == expected_author for chapter in result.chapters)
    assert all(not chapter.title[:1].isdigit() for chapter in result.chapters)


def test_mixed_artist_album_keeps_explicit_authors_and_does_not_guess_missing_ones() -> (
    None
):
    result = resolve_youtube_album_metadata(
        {
            "title": "sewerslvt / cynthoni mix to rot in your room with [REMASTERED]",
            "uploader": "pawsome :3",
            "description": "Music by:\nhttps://youtube.com/@Sewerslvt",
        },
        chapters(
            "01: Purple Hearts in her eyes",
            "cynthoni - Lychee Ice",
            "Nikita Kryukov - i'll be here for a while",
        ),
    )

    assert result.title == "Mix to rot in your room with [REMASTERED]"
    assert result.author == "sewerslvt, cynthoni"
    assert result.asset_author is None
    assert [(chapter.title, chapter.author) for chapter in result.chapters] == [
        ("Purple Hearts in her eyes", None),
        ("Lychee Ice", "cynthoni"),
        ("i'll be here for a while", "Nikita Kryukov"),
    ]


def test_unstructured_album_falls_back_without_promoting_uploader_to_artist() -> None:
    result = resolve_youtube_album_metadata(
        {"title": "late night favorites", "uploader": "random archive channel"},
        chapters("Intro", "Something Else"),
    )

    assert result.title == "late night favorites"
    assert result.author is result.asset_author is None
    assert [(chapter.title, chapter.author) for chapter in result.chapters] == [
        ("Intro", None),
        ("Something Else", None),
    ]


def test_provider_and_labeled_description_metadata_have_strict_precedence() -> None:
    provider = resolve_youtube_album_metadata(
        {
            "title": "Uploader title",
            "artists": [{"name": "One"}, {"name": "Two"}],
            "album": "Catalog album",
            "uploader": "Archive channel",
        },
        chapters("01: First", "02: Two - Second"),
    )
    labeled = resolve_youtube_album_metadata(
        {
            "title": "Unstructured upload",
            "description": "Artist: Exact Artist\nAlbum: Exact Album",
            "uploader": "Archive channel",
        },
        chapters("01: First", "02: Second"),
    )

    assert (provider.title, provider.author, provider.asset_author) == (
        "Catalog album",
        "One, Two",
        None,
    )
    assert provider.chapters[0].author is None
    assert provider.chapters[1].author == "Two"
    assert (labeled.title, labeled.author, labeled.asset_author) == (
        "Exact Album",
        "Exact Artist",
        "Exact Artist",
    )
