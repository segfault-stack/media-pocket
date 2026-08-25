from __future__ import annotations

import json
from collections.abc import AsyncIterator

from downloader_bot.domain import ErrorCode, JobStage, Progress


class RedisJobQueue:
    def __init__(
        self, redis, *, stream: str = "downloads", group: str = "download-workers"
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._group = group

    async def initialize(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, job_id: str) -> str:
        result = await self._redis.xadd(self._stream, {"job_id": job_id})
        return _text(result)

    async def consume(
        self, consumer: str, *, block_ms: int = 5_000
    ) -> AsyncIterator[tuple[str, str]]:
        records = await self._redis.xreadgroup(
            self._group, consumer, {self._stream: ">"}, count=10, block=block_ms
        )
        for _, messages in records:
            for message_id, fields in messages:
                yield (
                    _text(message_id),
                    _text(fields.get(b"job_id", fields.get("job_id"))),
                )

    async def ack(self, message_id: str) -> None:
        await self._redis.xack(self._stream, self._group, message_id)

    async def reclaim(
        self, consumer: str, *, idle_ms: int
    ) -> tuple[tuple[str, str], ...]:
        result = await self._redis.xautoclaim(
            self._stream, self._group, consumer, idle_ms, "0-0", count=100
        )
        messages = result[1]
        return tuple(
            (_text(message_id), _text(fields.get(b"job_id", fields.get("job_id"))))
            for message_id, fields in messages
        )


class RedisProgressBus:
    def __init__(
        self, redis, *, stream: str = "download-progress", group: str = "bot-progress"
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._group = group

    async def initialize(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="$", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, progress: Progress) -> None:
        payload = {
            "job_id": progress.job_id,
            "stage": progress.stage.value,
            "percent": progress.percent,
            "attempt": progress.attempt,
            "attempt_limit": progress.attempt_limit,
            "item": progress.item,
            "item_count": progress.item_count,
            "queue_position": progress.queue_position,
            "detail": progress.detail,
            "error_code": progress.error_code.value if progress.error_code else None,
            "downloaded_bytes": progress.downloaded_bytes,
            "total_bytes": progress.total_bytes,
            "total_bytes_is_estimate": progress.total_bytes_is_estimate,
            "speed_bytes_per_second": progress.speed_bytes_per_second,
            "eta_seconds": progress.eta_seconds,
            "elapsed_seconds": progress.elapsed_seconds,
            "indeterminate": progress.indeterminate,
            "occurred_at": progress.occurred_at.isoformat(),
        }
        await self._redis.xadd(
            self._stream,
            {"payload": json.dumps(payload)},
            maxlen=50_000,
            approximate=True,
        )

    async def consume(self, consumer: str) -> AsyncIterator[Progress]:
        records = await self._redis.xreadgroup(
            self._group, consumer, {self._stream: ">"}, count=50, block=5_000
        )
        for _, messages in records:
            for message_id, fields in messages:
                raw = _text(fields.get(b"payload", fields.get("payload")))
                data = json.loads(raw)
                yield Progress(
                    job_id=data["job_id"],
                    stage=JobStage(data["stage"]),
                    percent=data["percent"],
                    attempt=data["attempt"],
                    attempt_limit=data.get("attempt_limit", 3),
                    item=data["item"],
                    item_count=data["item_count"],
                    queue_position=data.get("queue_position"),
                    detail=data["detail"],
                    error_code=ErrorCode(data["error_code"])
                    if data.get("error_code")
                    else None,
                    downloaded_bytes=data.get("downloaded_bytes"),
                    total_bytes=data.get("total_bytes"),
                    total_bytes_is_estimate=data.get(
                        "total_bytes_is_estimate", False
                    ),
                    speed_bytes_per_second=data.get("speed_bytes_per_second"),
                    eta_seconds=data.get("eta_seconds"),
                    elapsed_seconds=data.get("elapsed_seconds"),
                    indeterminate=data.get("indeterminate", False),
                )
                await self._redis.xack(self._stream, self._group, message_id)


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
