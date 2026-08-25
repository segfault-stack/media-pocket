from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from downloader_bot.bootstrap.runtime import (
    _cleanup_loop,
    _heartbeat_loop,
    _outbox_loop,
    _present_progress,
    _process_message,
)
from downloader_bot.domain import Job, JobStage, Progress


class Queue:
    def __init__(self) -> None:
        self.acked = []
        self.published = []

    async def ack(self, value):
        self.acked.append(value)

    async def publish(self, value):
        self.published.append(value)


@pytest.mark.asyncio
async def test_worker_message_retries_then_acknowledges(monkeypatch) -> None:
    queue = Queue()

    class Process:
        async def execute(self, _job_id):
            return Job(
                "job",
                1,
                1,
                "https://example.com",
                "key",
                stage=JobStage.RETRYING,
                attempt=2,
            )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("downloader_bot.bootstrap.runtime.asyncio.sleep", no_sleep)
    await _process_message(SimpleNamespace(process=Process(), queue=queue), "1-0", "job")
    assert queue.published == ["job"] and queue.acked == ["1-0"]


@pytest.mark.asyncio
async def test_present_progress_updates_status_and_delivers_ready() -> None:
    job = Job("job", 1, 2, "https://example.com", "key", stage=JobStage.READY)

    class Jobs:
        def __init__(self) -> None:
            self.transitions = []

        async def get(self, _job_id):
            return job

        async def transition(self, *args, **kwargs):
            self.transitions.append((args, kwargs))

    class Gateway:
        async def show_status(self, _job, _progress):
            return 9

    class Deliver:
        def __init__(self) -> None:
            self.values = []

        async def execute(self, job_id):
            self.values.append(job_id)

    container = SimpleNamespace(jobs=Jobs(), refresh_parent=SimpleNamespace())
    deliver = Deliver()
    await _present_progress(
        container,
        Gateway(),
        deliver,
        {},
        Progress(job.id, JobStage.READY, 100),
    )
    assert container.jobs.transitions and deliver.values == [job.id]


@pytest.mark.asyncio
async def test_parent_progress_is_aggregated() -> None:
    child = Job(
        "child", 1, 2, "https://example.com", "key", parent_id="parent", stage=JobStage.DOWNLOADING
    )
    parent = Job(
        "parent", 1, 2, "batch://links", "parent-key", is_parent=True, children_total=2, stage=JobStage.DOWNLOADING
    )

    class Jobs:
        async def get(self, _job_id):
            return child

        async def transition(self, *_args, **_kwargs):
            return None

    class Refresh:
        async def execute(self, _parent_id):
            return parent

    seen = []

    class Gateway:
        async def show_status(self, job, progress):
            seen.append((job, progress))

    await _present_progress(
        SimpleNamespace(jobs=Jobs(), refresh_parent=Refresh()),
        Gateway(),
        SimpleNamespace(execute=None),
        {},
        Progress(child.id, JobStage.DOWNLOADING, 40),
    )
    assert seen[0][0].id == parent.id and seen[0][1].item_count == 2


@pytest.mark.asyncio
async def test_background_iterations_are_cancellable(monkeypatch, tmp_path) -> None:
    calls = []

    async def stop(_seconds):
        raise RuntimeError("stop")

    monkeypatch.setattr("downloader_bot.bootstrap.runtime.asyncio.sleep", stop)

    class Publisher:
        async def execute(self):
            calls.append("outbox")
            return 1

    with pytest.raises(RuntimeError, match="stop"):
        await _outbox_loop(SimpleNamespace(publish_outbox=Publisher()))

    class Cleanup:
        async def execute(self, **_kwargs):
            calls.append("cleanup")

    container = SimpleNamespace(
        cleanup_artifacts=Cleanup(),
        settings=SimpleNamespace(artifact_retention_seconds=1),
    )
    with pytest.raises(RuntimeError, match="stop"):
        await _cleanup_loop(container)
    with pytest.raises(RuntimeError, match="stop"):
        await _heartbeat_loop(Path(tmp_path / "heartbeat"))
    assert calls == ["outbox", "cleanup"]
