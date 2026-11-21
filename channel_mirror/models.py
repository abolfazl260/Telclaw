"""Transport-independent contracts for Channel Mirror V1."""

from dataclasses import dataclass
from datetime import datetime, timezone
import re


class ValidationError(ValueError):
    pass


def stamp(value=None):
    value = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("An aware datetime is required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def telegram_id(value, *, positive=False):
    if type(value) is not int or not -(2**63) <= value < 2**63 or value == 0 or (positive and value < 0):
        raise ValidationError("A nonzero signed 64-bit Telegram ID is required")
    return value


def required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required")
    return value.strip()


def caption(username, footer):
    username = required(username, "Public destination @username")
    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", username):
        raise ValidationError("A public destination @username is required")
    if not isinstance(footer, str):
        raise ValidationError("Footer must be fixed text")
    result = username + ("\n\n" + footer if footer else "")
    if len(result.encode("utf-16-le")) // 2 > 1024:
        raise ValidationError("Destination caption exceeds 1024 UTF-16 units")
    return result


@dataclass(frozen=True)
class SourceItem:
    message_id: int
    date: datetime
    media_kind: str
    grouped_id: int | None = None

    def __post_init__(self):
        telegram_id(self.message_id, positive=True)
        stamp(self.date)
        if self.media_kind not in {"photo", "video", "document"}:
            raise ValidationError("V1 supports photos, videos and documents only")
        if self.grouped_id is not None:
            telegram_id(self.grouped_id)


@dataclass(frozen=True)
class SourcePost:
    source_peer_id: int
    items: tuple[SourceItem, ...]
    crawl_run_id: str
    album_complete: bool = False

    def __post_init__(self):
        telegram_id(self.source_peer_id)
        required(self.crawl_run_id, "Crawl run identity")
        items = tuple(self.items)
        if not items or any(not isinstance(item, SourceItem) for item in items):
            raise ValidationError("Media items are required")
        items = tuple(sorted(items, key=lambda item: item.message_id))
        if len({item.message_id for item in items}) != len(items):
            raise ValidationError("Repeated album member")
        groups = {item.grouped_id for item in items}
        if groups == {None}:
            if len(items) != 1:
                raise ValidationError("Ungrouped media must be submitted as individual posts")
        elif (None in groups or len(groups) != 1 or not self.album_complete
              or not 2 <= len(items) <= 10
              or any(item.media_kind not in {"photo", "video"} for item in items)):
            raise ValidationError("Submit a complete photo/video album with 2–10 members")
        object.__setattr__(self, "items", items)

    @property
    def identity(self):
        first = self.items[0]
        return f"album:{first.grouped_id}" if first.grouped_id is not None else f"message:{first.message_id}"


@dataclass(frozen=True)
class SendOutcome:
    """retry means definitely NOT sent; ambiguity must use uncertain.

    IDs from a partial album are preserved and force uncertain, even if the
    transport reports retry/failed. Only a complete ID vector can be sent.
    """

    status: str
    message_ids: tuple[int, ...] = ()
    error: str = ""
    retry_after: int = 0

    def __post_init__(self):
        if self.status not in {"sent", "retry", "failed", "uncertain", "skipped"}:
            raise ValidationError("Invalid delivery outcome")
        ids = tuple(self.message_ids)
        for mid in ids:
            telegram_id(mid, positive=True)
        if len(set(ids)) != len(ids):
            raise ValidationError("Destination message IDs must be unique")
        if type(self.retry_after) is not int or self.retry_after < 0:
            raise ValidationError("retry_after must be nonnegative seconds")
        if not isinstance(self.error, str):
            raise ValidationError("Error must be text")
        object.__setattr__(self, "message_ids", ids)
