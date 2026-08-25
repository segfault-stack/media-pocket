from __future__ import annotations

import pytest

from downloader_bot.domain.audio_names import (
    build_audio_filename,
    normalize_artist_names,
    normalize_audio_title,
    resolve_audio_title_artist,
    split_audio_artist_title,
)


def test_artist_normalization_keeps_legacy_deduplication_and_real_names() -> None:
    assert normalize_artist_names(
        [{"name": "SUDNO"}, " sudno ", {"name": "Sudno"}, "SUDNO"]
    ) == "SUDNO"
    assert normalize_artist_names("SUDNO, sudno, SUDNO") == "SUDNO"
    assert normalize_artist_names("Tyler, The Creator") == "Tyler, The Creator"
    assert normalize_artist_names(["Artist One", "Artist Two", "artist one"]) == (
        "Artist One, Artist Two"
    )
    assert normalize_artist_names(" Crystal\u200b Castles - Topic ") == "Crystal Castles"


def test_artist_title_split_is_deliberately_conservative() -> None:
    assert split_audio_artist_title("SOPHIE- My Forever") == (
        "SOPHIE",
        "My Forever",
    )
    assert split_audio_artist_title("SOPHIE -My Forever") == (
        "SOPHIE",
        "My Forever",
    )
    assert split_audio_artist_title("SOPHIE — My Forever") == (
        "SOPHIE",
        "My Forever",
    )
    assert split_audio_artist_title("AC-DC") is None
    assert split_audio_artist_title("Twenty-one Pilots") is None


def test_audio_title_removes_repeated_fuzzy_artist_and_provider_noise() -> None:
    assert normalize_audio_title(
        "Crystal Castles - Crystal Castles - Transgender",
        "Crystal Castles",
    ) == "Transgender"
    assert normalize_audio_title("Crystal Castle - Transgender", "Crystal Castles") == (
        "Transgender"
    )
    assert normalize_audio_title("Beyonce - Halo", "Beyoncé") == "Halo"
    assert normalize_audio_title(
        "Guest feat. Artist - Song", ["Artist", "Guest"]
    ) == "Song"
    assert normalize_audio_title(
        "Transgender [Official Audio]", "Crystal Castles"
    ) == "Transgender"
    assert normalize_audio_title("Transgender - Lyrics", "Crystal Castles") == (
        "Transgender"
    )
    assert normalize_audio_title("Official Audio Memories", "Artist") == (
        "Official Audio Memories"
    )
    assert normalize_audio_title("AB - Song", "CD") == "AB - Song"
    assert normalize_audio_title("!!! - Song", "Artist") == "!!! - Song"
    assert normalize_audio_title("[Official Audio]", "Artist") == "[Official Audio]"


@pytest.mark.parametrize(
    ("artist", "title"),
    [
        ("Crystal Castles", "Crystal Fighters - Midnight Song"),
        ("The Weeknd", "Weekend Players — Live Session"),
        ("Arctic Monkeys", "Arctic Lake | Extended Mix"),
        ("Tame Impala", "Tame Tiger : Studio Demo"),
        ("Daft Punk", "Daft Club - Official Audio Memories"),
        ("The Cure", "The Cult - Midnight Song"),
        ("Radiohead", "Razorlight — Live Session"),
        ("Massive Attack", "Sneaker Pimps | Extended Mix"),
        ("Portishead", "Morcheeba : Studio Demo"),
        ("Depeche Mode", "Duran Duran - Official Audio Memories"),
    ],
)
def test_fuzzy_matching_does_not_strip_distinct_artist_prefixes(artist, title) -> None:
    assert normalize_audio_title(title, artist) == title


@pytest.mark.parametrize(
    ("channel", "raw_title", "expected"),
    [
        ("The Weeknd", "The Weeknd - Blinding Lights (Official Audio)", "Blinding Lights"),
        ("Alice In Chains", "Alice In Chains - Nutshell (Official Audio)", "Nutshell"),
        ("Atlantic Records", "Skillet - Awake and Alive (Official Audio)", "Skillet - Awake and Alive"),
        ("7clouds", "David Guetta - Titanium (Lyrics) ft. Sia", "David Guetta - Titanium (Lyrics) ft. Sia"),
        ("Coldplay", "Coldplay - A Sky Full Of Stars (Live at River Plate)", "A Sky Full Of Stars (Live at River Plate)"),
        ("HYBE LABELS", 'KATSEYE (캣츠아이) "Animal" Official MV', 'KATSEYE (캣츠아이) "Animal" Official MV'),
        ("Foals", "Foals - Late Night [Solomun Remix] (Official Audio)", "Late Night [Solomun Remix]"),
        (
            "Gunna",
            "Gunna - endless [Official Visualizer]",
            "endless [Official Visualizer]",
        ),
    ],
)
def test_real_youtube_title_variants_from_the_legacy_regression_table(
    channel, raw_title, expected
) -> None:
    assert normalize_audio_title(raw_title, channel) == expected


def test_metadata_resolution_precedence_and_safe_filename_generation() -> None:
    assert resolve_audio_title_artist(
        {"title": "SOPHIE- My Forever (Official Audio)", "uploader": "Wrong"}
    ) == ("My Forever", "SOPHIE")
    assert resolve_audio_title_artist(
        {"track": "Artist - Song", "artist": "Artist", "uploader": "Wrong"}
    ) == ("Song", "Artist")
    assert resolve_audio_title_artist({"title": "Song", "uploader": "Channel"}) == (
        "Song",
        "Channel",
    )
    filename = build_audio_filename(
        "A" * 300 + " [Official Audio]", "Artist/Name", suffix="m4a"
    )
    assert filename.endswith(".m4a")
    assert "/" not in filename
    assert len(filename.encode()) <= 180
