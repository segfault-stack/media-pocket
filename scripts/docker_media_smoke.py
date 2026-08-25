"""Verify the container's media toolchain without external network access."""

from __future__ import annotations

import importlib
import json
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from yt_dlp import YoutubeDL


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        pass


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def main() -> None:
    for module in ("aiogram", "asyncpg", "httpx", "psycopg2", "redis", "sqlalchemy"):
        importlib.import_module(module)

    run("ffmpeg", "-version")
    run("ffprobe", "-version")
    run("deno", "eval", "if (1 + 1 !== 2) Deno.exit(1)")

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "source.m4a"
        run(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=0.25",
            "-c:a",
            "aac",
            str(source),
        )

        handler = partial(QuietHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with YoutubeDL(
                {
                    "format": "bestaudio/best",
                    "outtmpl": str(root / "result.%(ext)s"),
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "320",
                        }
                    ],
                    "quiet": True,
                    "noprogress": True,
                }
            ) as ydl:
                ydl.download([f"http://127.0.0.1:{server.server_port}/{source.name}"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        output = root / "result.mp3"
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("yt-dlp did not create a non-empty MP3")

        probe = run(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            str(output),
        )
        codecs = {stream["codec_name"] for stream in json.loads(probe.stdout)["streams"]}
        if codecs != {"mp3"}:
            raise RuntimeError(f"unexpected output codecs: {sorted(codecs)}")

    print("Docker media smoke test passed: yt-dlp, FFmpeg, FFprobe, Deno, and native Python modules work.")


if __name__ == "__main__":
    main()
