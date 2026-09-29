"""Regression tests for skipping posts forwarded from Telegram channels."""

import sqlite3
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerChannel, PeerUser

from collection import crawler


def _message(message_id, forward_from):
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc),
        fwd_from=SimpleNamespace(from_id=forward_from),
        sender=SimpleNamespace(
            id=9000 + message_id,
            username=f"user{message_id}",
            bot=False,
            broadcast=False,
        ),
        text="this forwarded personal message has enough words to pass the minimum collection text threshold",
        raw_text="this forwarded personal message has enough words to pass the minimum collection text threshold",
        message="this forwarded personal message has enough words to pass the minimum collection text threshold",
        media=None,
    )


def test_forward_origin_classifier_only_rejects_channels():
    assert crawler._is_forwarded_from_channel(
        SimpleNamespace(fwd_from=SimpleNamespace(from_id=PeerChannel(123)))
    )
    assert not crawler._is_forwarded_from_channel(
        SimpleNamespace(fwd_from=SimpleNamespace(from_id=PeerUser(456)))
    )
    assert not crawler._is_forwarded_from_channel(SimpleNamespace(fwd_from=None))


@pytest.mark.asyncio
async def test_channel_forward_is_skipped_before_persistence_but_user_forward_is_saved(monkeypatch):
    channel_forward = _message(1, PeerChannel(111))
    user_forward = _message(2, PeerUser(222))
    saved = []

    class FakeMessageService:
        def initialize(self):
            return None

        def save_collected_message(self, **message):
            saved.append(message)
            return True

    class FakeClient:
        async def get_input_entity(self, _username):
            return SimpleNamespace(channel_id=77, title="Source")

        async def iter_messages(self, _entity):
            yield channel_forward
            yield user_forward

    def fake_connection():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT,
                message_id INTEGER,
                text TEXT,
                sender_id INTEGER
            )"""
        )
        return conn

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(crawler, "MessageService", FakeMessageService)
    monkeypatch.setattr(crawler, "get_connection", fake_connection)
    monkeypatch.setattr(crawler.asyncio, "sleep", no_sleep)

    result = await crawler.crawl_channel(
        FakeClient(),
        "source_channel",
        date(2026, 9, 29),
        date(2026, 9, 29),
    )

    assert result["forwarded_channel_skipped"] == 1
    assert result["saved"] == 1
    assert [item["message_id"] for item in saved] == [2]
