from dataclasses import dataclass

from downloader_bot.domain import JobStage, Progress

from .ports import Clock


@dataclass(slots=True)
class ProgressThrottle:
    clock: Clock
    min_percent_delta: int = 2
    max_interval_seconds: float = 2.0
    _last: Progress | None = None
    _last_time: float = 0.0

    def accept(self, progress: Progress) -> bool:
        now = self.clock.monotonic()
        stage_changed = self._last is None or self._last.stage is not progress.stage
        percent_changed = (
            self._last is None
            or abs(progress.percent - self._last.percent) >= self.min_percent_delta
        )
        expired = (
            self._last is None or now - self._last_time >= self.max_interval_seconds
        )
        terminal = progress.stage in {
            JobStage.DELIVERED,
            JobStage.CANCELLED,
            JobStage.FAILED,
        }
        if stage_changed or percent_changed or expired or terminal:
            self._last = progress
            self._last_time = now
            return True
        return False
