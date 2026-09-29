"""Regression tests for accepting only verifiable human forwarded origins."""

import sqlite3
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerChannel, PeerUser

from collection import crawler


def _message(message_id, forward_from=None):
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc),
        fwd_from=None if forward_from is None else SimpleNamespace(from_id=forward_from),
        forward=None,
        sender=SimpleNamespace(
            id=9000 + message_id,
            username=f"user{message_id}",
            bot=False,
            broadcast=False,
        ),
        text="this personal message has enough words to pass the minimum collection text threshold safely",
        raw_text="this personal message has enough words to pass the minimum collection text threshold safely",
        message="this personal message has enough words to pass the minimum collection text threshold safely",
        media=None,
    )


@pytest.mark.asyncio
async def test_forward_origin_classifier_distinguishes_human_bot_channel_and_unknown():
    class FakeClient:
        async def get_entity(self, peer):
            if isinstance(peer, PeerUser) and peer.user_id == 10:
                return SimpleNamespace(id=10, bot=False)
            if isinstance(peer, PeerUser) and peer.user_id == 20:
                return SimpleNamespace(id=20, bot=True)
            raise ValueError("unresolvable origin")

    client = FakeClient()

    assert await crawler._forward_origin_kind(client, _message(1, None)) is None
    assert await crawler._forward_origin_kind(client, _message(2, PeerUser(10))) == "user"
    assert await crawler._forward_origin_kind(client, _message(3, PeerUser(20))) == "bot"
    assert await crawler._forward_origin_kind(client, _message(4, PeerChannel(30))) == "channel"
    assert await crawler._forward_origin_kind(
        client, SimpleNamespace(fwd_from=SimpleNamespace(from_id=None), forward=None)
    ) == "unknown"


@pytest.mark.asyncio
async def test_only_direct_human_and_forwarded_human_reach_persistence(monkeypatch):
    direct_human = _message(1, None)
    forwarded_human = _message(2, PeerUser(10))
    forwarded_bot = _message(3, PeerUser(20))
    forwarded_channel = _message(4, PeerChannel(30))
    forwarded_unknown = _message(5, PeerUser(40))
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

        async def get_entity(self, peer):
            if isinstance(peer, PeerUser) and peer.user_id == 10:
                return SimpleNamespace(id=10, bot=False)
            if isinstance(peer, PeerUser) and peer.user_id == 20:
                return SimpleNamespace(id=20, bot=True)
            raise ValueError("unresolvable origin")

        async def iter_messages(self, _entity):
            yield direct_human
            yield forwarded_human
            yield forwarded_bot
            yield forwarded_channel
            yield forwarded_unknown

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

    assert result["saved"] == 2
    assert result["forwarded_bot_skipped"] == 1
    assert result["forwarded_channel_skipped"] == 1
    assert result["forwarded_unknown_skipped"] == 1
    assert [item["message_id"] for item in saved] == [1, 2]


@pytest.mark.asyncio
async def test_forward_object_sender_is_used_without_extra_lookup():
    message = _message(1, PeerUser(99))
    message.forward = SimpleNamespace(sender=SimpleNamespace(id=99, bot=True))

    class NoLookupClient:
        async def get_entity(self, _peer):
            raise AssertionError("cached forward sender should be used")

    assert await crawler._forward_origin_kind(NoLookupClient(), message) == "bot"
