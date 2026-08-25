from urllib.parse import parse_qs, urlsplit

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_VIDEO_PATH_PREFIXES = ("/embed/", "/live/", "/shorts/")


def has_youtube_playlist(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not any(host == item or host.endswith(f".{item}") for item in _YOUTUBE_HOSTS):
        return False
    return bool(parse_qs(parsed.query).get("list", [""])[0].strip())


def youtube_playlist_has_single_video(url: str) -> bool:
    if not has_youtube_playlist(url):
        return False
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if host == "youtu.be" and path not in {"", "/"}:
        return True
    if parse_qs(parsed.query).get("v", [""])[0].strip():
        return True
    return any(
        path.startswith(prefix) and len(path) > len(prefix)
        for prefix in _VIDEO_PATH_PREFIXES
    )
