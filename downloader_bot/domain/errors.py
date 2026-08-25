from dataclasses import dataclass

from .models import ErrorCode


@dataclass(slots=True)
class DownloadError(Exception):
    code: ErrorCode
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message
