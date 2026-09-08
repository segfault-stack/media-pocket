from urllib.parse import urljoin

from yt_dlp.extractor.mailru import MailRuMusicIE
from yt_dlp.utils import ExtractorError, int_or_none, parse_duration, url_or_none


class _MediaPocketMailRuMusicIE(MailRuMusicIE, plugin_name="media_pocket"):
    """Resolve Mail.ru music from the page's fresh, signed track payload."""

    def _real_extract(self, url: str) -> dict:
        audio_id = self._match_id(url)
        webpage = self._download_webpage(url, audio_id)
        page_config = self._search_json(
            r'<script[^>]+class=["\'][^"\']*\bb-page-config\b[^"\']*["\'][^>]*>',
            webpage,
            "page config",
            audio_id,
            end_pattern=r"</script>",
        )
        track = page_config.get("track")
        if not isinstance(track, dict) or track.get("file") != audio_id:
            raise ExtractorError("Unable to find the requested track in page config")

        media_url = url_or_none(urljoin(url, str(track.get("url") or "")))
        if not media_url:
            raise ExtractorError("The track page did not provide a media URL")

        track_name = track.get("name") or track.get("nameTextHtml")
        artist = track.get("author") or track.get("authorTextHtml")
        nested_artist = track.get("artist")
        if not artist and isinstance(nested_artist, dict):
            artist = nested_artist.get("name")
        title = f"{artist} - {track_name}" if artist and track_name else track_name

        return {
            "id": audio_id,
            "title": title or audio_id,
            "track": track_name,
            "artist": artist,
            "album": track.get("album"),
            "thumbnail": url_or_none(
                track.get("albumCoverURL") or track.get("filedAlbumCoverURL")
            ),
            "uploader": track.get("ownerName"),
            "uploader_id": str(track["uploaderId"])
            if track.get("uploaderId") is not None
            else None,
            "duration": int_or_none(track.get("durationInSeconds"))
            or parse_duration(track.get("durationStr")),
            "view_count": int_or_none(track.get("playCount")),
            "vcodec": "none",
            "abr": 320 if track.get("isHQ") else 128,
            "url": media_url,
            "http_headers": {"Referer": url},
        }
