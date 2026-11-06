from dataclasses import dataclass
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utcnow()).astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SendResult:
    status: str
    error: str = ""
    message_id: int | None = None
    message_link: str = ""
    file_id: str = ""
    retry_after: int = 0
