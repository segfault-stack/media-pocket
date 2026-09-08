from yt_dlp import YoutubeDL

from yt_dlp_plugins.extractor.media_pocket_mailru import (
    _MediaPocketMailRuMusicIE,
)

MAILRU_URL = (
    "https://my.mail.ru/music/songs/"
    "al-90-dx2ov-camo-2878c47175fa5841b5f1afefbf543f14"
)


def test_mailru_music_plugin_uses_signed_track_data_from_page(monkeypatch) -> None:
    webpage = """
        <script type="text/plain" class="b-page-config">
        {
          "track": {
            "file": "2878c47175fa5841b5f1afefbf543f14",
            "url": "//moosic.my.mail.ru/file/track.mp3?k2=fresh-signature",
            "name": "CAMO",
            "author": "AL-90 & DX2OV",
            "album": "",
            "uploaderId": "1828536422",
            "ownerName": "Ilya Cherkas",
            "durationInSeconds": 272,
            "playCount": 12,
            "isHQ": false
          }
        }
        </script>
    """
    extractor = _MediaPocketMailRuMusicIE(YoutubeDL({"quiet": True}))
    monkeypatch.setattr(
        extractor,
        "_download_webpage",
        lambda url, audio_id: webpage,
    )

    info = extractor._real_extract(MAILRU_URL)

    assert info["url"] == (
        "https://moosic.my.mail.ru/file/track.mp3?k2=fresh-signature"
    )
    assert info["title"] == "AL-90 & DX2OV - CAMO"
    assert info["track"] == "CAMO"
    assert info["artist"] == "AL-90 & DX2OV"
    assert info["uploader"] == "Ilya Cherkas"
    assert info["duration"] == 272
    assert info["http_headers"] == {"Referer": MAILRU_URL}
