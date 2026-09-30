"""Collection-time raw text, metadata and legacy-storage compatibility contracts.

All crawling uses in-memory Telegram fakes and an isolated temporary SQLite DB.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerUser

import config
from collection import crawler
from processing.processing_service import ProcessingService
from storage import database
from storage.content_duplicate import content_hash
from storage.message_repository import MessageRepository


CRAWL_DATE = date(2026, 9, 29)
ORIGINAL_TEXT = (
    "سلام   دنیا 😀\n"
    "ارسال  بار از تهران به تورنتو   تاریخ فردا تماس بگیرید"
)
PHOTO_CAPTION = (
    "Cargo   📦 from Berlin\n to Toronto   ten boxes available "
    "tomorrow please contact my telegram account"
)


def fake_message(message_id, payload, *, photo=False, forwarded=False, text_fallback=False):
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 9, 29, 8, tzinfo=timezone.utc),
        sender=SimpleNamespace(
            id=9000 + message_id,
            username=f"user_{message_id}",
            bot=False,
            broadcast=False,
        ),
        fwd_from=(
            SimpleNamespace(from_id=PeerUser(801)) if forwarded else None
        ),
        forward=None,
        text="" if text_fallback else payload,
        raw_text=payload,
        message=payload,
        media=(
            SimpleNamespace(photo=SimpleNamespace(id=123456 + message_id), document=None)
            if photo else None
        ),
        grouped_id=777 if photo else None,
    )


class FakeTelegramClient:
    def __init__(self, messages):
        self.messages = list(messages)

    async def get_input_entity(self, _channel):
        return SimpleNamespace(channel_id=42, title="Local fixture source")

    async def get_entity(self, _peer):
        return SimpleNamespace(bot=False)

    async def iter_messages(self, _entity):
        for message in self.messages:
            yield message


@pytest.fixture
def isolated_crawler_db(tmp_path, monkeypatch):
    path = tmp_path / "raw-crawler.sqlite3"
    monkeypatch.setattr(config, "DB_NAME", str(path))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(crawler.asyncio, "sleep", no_sleep)
    return path


@pytest.mark.asyncio
async def test_crawl_preserves_exact_original_text_and_photo_caption(
    isolated_crawler_db,
):
    assert crawler._has_minimum_message_text(ORIGINAL_TEXT)
    assert crawler._has_minimum_message_text(PHOTO_CAPTION)

    client = FakeTelegramClient(
        [
            fake_message(101, ORIGINAL_TEXT),
            fake_message(
                102,
                PHOTO_CAPTION,
                photo=True,
                text_fallback=True,
            ),
            fake_message(103, ORIGINAL_TEXT, forwarded=True),
        ]
    )

    result = await crawler.crawl_channel(
        client, "fixture_source", CRAWL_DATE, CRAWL_DATE
    )

    assert result["status"] == "completed"
    assert result["saved"] == 3
    assert result["media_saved"] == 1

    with database.get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM messages ORDER BY message_id"
            ).fetchall()
        ]

    assert [row["message_id"] for row in rows] == [101, 102, 103]
    assert [row["raw_text"] for row in rows] == [
        ORIGINAL_TEXT,
        PHOTO_CAPTION,
        ORIGINAL_TEXT,
    ]
    for row in rows:
        assert row["text"] == row["raw_text"]
        assert row["cleaned_text"] is None
        assert row["collection_status"] == "collected"
        assert row["processing_status"] == "pending"
        assert row["ai_status"] == "waiting"
        assert row["content_hash"] == content_hash(row["raw_text"])
        assert row["media_path"] is None
        assert row["cleaned_at"] is None

    photo = rows[1]
    assert photo["has_media"] == 1
    assert photo["media_type"] == "photo"
    assert photo["media_group_id"] == "777"
    assert photo["file_unique_id"] == "123558"
    assert photo["message_link"] == "https://t.me/fixture_source/102"
    assert photo["media_reference"] == "telegram://fixture_source/102"


@pytest.mark.asyncio
async def test_recrawl_does_not_change_previous_text_or_duplicate_records(
    isolated_crawler_db,
):
    client = FakeTelegramClient([fake_message(201, ORIGINAL_TEXT)])

    first = await crawler.crawl_channel(
        client, "fixture_source", CRAWL_DATE, CRAWL_DATE
    )
    assert first["saved"] == 1

    with database.get_connection() as conn:
        original_row = dict(
            conn.execute(
                "SELECT * FROM messages WHERE message_id=201"
            ).fetchone()
        )

    second = await crawler.crawl_channel(
        client, "fixture_source", CRAWL_DATE, CRAWL_DATE
    )
    assert second["saved"] == 0
    assert second["duplicates_skipped"] == 1

    with database.get_connection() as conn:
        records = conn.execute(
            "SELECT * FROM messages WHERE message_id=201"
        ).fetchall()

    assert len(records) == 1
    assert dict(records[0]) == original_row


def test_repository_insert_keeps_legacy_text_only_message_readable(
    isolated_crawler_db,
):
    database.initialize_db()
    legacy_text = "Original   content\nwith  extra whitespace"
    assert database.insert_message(
        "legacy_source",
        7,
        legacy_text,
        "2026-09-29",
        raw_text=None,
        cleaned_text=None,
        sender_id=501,
    )
    assert not database.insert_message(
        "legacy_source",
        7,
        "attempted overwrite",
        "2026-09-29",
        raw_text="attempted raw overwrite",
        cleaned_text="attempted cleaned overwrite",
        sender_id=501,
    )

    history = MessageRepository().get_previous_messages_by_sender(
        sender_id=501,
        before_id=999,
    )
    assert len(history) == 1
    assert history[0]["raw_text"] is None
    assert history[0]["text"] == legacy_text
    assert ProcessingService._original_text(history[0]) == legacy_text.strip()

    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT raw_text, text, cleaned_text FROM messages WHERE message_id=7"
        ).fetchone()
    assert tuple(row) == (None, legacy_text, None)


def test_previous_messages_prefer_raw_payload_to_edited_legacy_text(
    isolated_crawler_db,
):
    database.initialize_db()
    assert database.insert_message(
        "source",
        8,
        text="cleaned content with different spaces",
        date_str="2026-09-29",
        raw_text="raw   content with original spaces",
        cleaned_text="cleaned content with different spaces",
        sender_id=502,
    )

    history = MessageRepository().get_previous_messages_by_sender(
        sender_id=502, before_id=999
    )
    assert len(history) == 1
    assert ProcessingService._original_text(history[0]) == (
        "raw   content with original spaces"
    )
