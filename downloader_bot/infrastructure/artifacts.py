from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path, PurePosixPath

from downloader_bot.domain import DownloadArtifact, MediaKind


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    async def persist(
        self, job_id: str, artifacts: tuple[DownloadArtifact, ...]
    ) -> None:
        directory = self._root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        materialized: list[DownloadArtifact] = []
        for index, item in enumerate(artifacts):
            source = Path(item.path)
            thumbnail = Path(item.thumbnail_path) if item.thumbnail_path else None
            if source.parent.resolve() != directory.resolve():
                target = directory / f"cached-{index:03d}{source.suffix}"
                await asyncio.to_thread(shutil.copy2, source, target)
                item = replace(item, path=PurePosixPath(target))
            if thumbnail and thumbnail.parent.resolve() != directory.resolve():
                thumbnail_target = directory / f"cached-{index:03d}.thumbnail.jpg"
                await asyncio.to_thread(shutil.copy2, thumbnail, thumbnail_target)
                item = replace(item, thumbnail_path=PurePosixPath(thumbnail_target))
            materialized.append(item)
        payload = [
            {
                "path": str(item.path),
                "kind": item.kind.value,
                "size": item.size,
                "checksum": item.checksum,
                "mime_type": item.mime_type,
                "title": item.title,
                "author": item.author,
                "duration_ms": item.duration_ms,
                "thumbnail_path": str(item.thumbnail_path)
                if item.thumbnail_path
                else None,
            }
            for item in materialized
        ]
        await asyncio.to_thread(
            (directory / "manifest.json").write_text, json.dumps(payload), "utf-8"
        )

    async def get(self, job_id: str) -> tuple[DownloadArtifact, ...]:
        raw = await asyncio.to_thread(
            (self._root / job_id / "manifest.json").read_text, "utf-8"
        )
        return tuple(
            DownloadArtifact(
                path=PurePosixPath(item["path"]),
                kind=MediaKind(item["kind"]),
                size=item["size"],
                checksum=item["checksum"],
                mime_type=item["mime_type"],
                title=item.get("title"),
                author=item.get("author"),
                duration_ms=item.get("duration_ms"),
                thumbnail_path=PurePosixPath(item["thumbnail_path"])
                if item.get("thumbnail_path")
                else None,
            )
            for item in json.loads(raw)
        )

    async def cleanup(self, job_id: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._root / job_id, True)
