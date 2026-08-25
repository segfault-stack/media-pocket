from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def writable_cookie_file(source: str | None) -> Iterator[str | None]:
    """Give yt-dlp a disposable cookie jar without mutating broker snapshots."""
    if not source or not Path(source).is_file():
        yield source
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="downloader-cookies-", suffix=".txt"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        yield str(temporary)
    finally:
        temporary.unlink(missing_ok=True)
