"""Regression coverage for admin single-message Telegram crawling."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config
import backoffice_web
from backoffice_layout import page_header
from backoffice_manual_crawl import parse_message_reference, source_channels, _result_details
from backoffice_operations import BackofficeOperations
from collection import crawler
from storage.database import get_connection


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("12345", 12345),
        ("https://t.me/source_channel/12345", 12345),
        ("https://www.t.me/SOURCE_CHANNEL/12345?single", 12345),
    ],
)
def test_manual_crawl_accepts_numeric_id_or_matching_link(reference, expected):
    assert parse_message_reference(reference, "source_channel") == expected


@pytest.mark.parametrize("reference", [
    "0", "-2", "2147483648", "not a message",
    "https://evil.example/source_channel/123",
    "http://t.me/source_channel/123",
    "https://t.me/other_channel/123",
    "https://t.me/source_channel/123/another",
    "https://t.me/c/123456/123",
    "https://t.me/source_channel/123#other",
    "https://t.me/source_channel/123?comment=10",
])
def test_manual_crawl_rejects_untrusted_or_mismatched_links(reference):
    with pytest.raises(ValueError):
        parse_message_reference(reference, "source_channel")


def test_source_options_only_include_configured_public_channel_usernames():
    channels = {
        "jobs": [
            {"username": "@Source_Channel", "name": "Jobs & Careers"},
            {"username": "https://t.me/+secret"},
        ],
        "cargo": [{"username": "source_channel", "name": "Duplicate"}],
    }
    assert source_channels(channels) == [
        ("jobs", "source_channel", "Jobs & Careers")
    ]


def test_manual_crawl_is_linked_from_backoffice_navigation():
    app = backoffice_web.create_app()
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}
    assert ("GET", "/crawl/manual") in routes
    assert ("POST", "/crawl/manual") in routes
    assert 'href="/crawl/manual"' in page_header("manual_crawl", "Manual Crawl")


def test_manual_crawl_reports_filtered_posts_not_as_saved():
    assert "fewer than 10" in _result_details({"status": "completed", "weak_text": 1})
    assert "could not be found" in _result_details({"status": "not_found"})
    assert "Normal Processing" in _result_details({"status": "completed", "saved": 1})


class SingleMessageClient:
    def __init__(self, payload):
        self.payload = payload
        self.get_messages_calls = []
        self.iterated = False

    async def get_input_entity(self, channel):
        assert channel == "source_channel"
        return SimpleNamespace(channel_id=42, title="Test Channel")

    async def get_messages(self, entity, *, ids):
        self.get_messages_calls.append(ids)
        if self.payload is None:
            return None
        return SimpleNamespace(
            id=ids,
            date=datetime(2026, 9, 30, tzinfo=timezone.utc),
            sender=SimpleNamespace(id=2001, username="test_sender", bot=False, broadcast=False),
            fwd_from=None, forward=None, raw_text=self.payload, message=self.payload,
            media=None, grouped_id=None,
        )

    async def iter_messages(self, entity, **kwargs):
        self.iterated = True
        raise AssertionError("Single-message collection must not iterate channel history")
        yield None


@pytest.fixture
def manual_crawl_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "manual-crawl.sqlite3"))

    async def no_sleep(_delay):
        raise AssertionError("Single-message collection must not sleep between messages")

    monkeypatch.setattr(crawler.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_single_message_crawl_uses_existing_storage_and_deduplication(manual_crawl_db):
    text = "From Tehran to Toronto cargo delivery with available space please contact this account for details"
    client = SingleMessageClient(text)
    first = await crawler.crawl_message(client, "SOURCE_CHANNEL", 567)
    assert first["status"] == "completed"
    assert first["saved"] == 1
    assert client.get_messages_calls == [567]
    assert not client.iterated
    with get_connection() as conn:
        stored = dict(conn.execute(
            "SELECT * FROM messages WHERE channel_username='source_channel' AND message_id=567"
        ).fetchone())
    assert stored["raw_text"] == text
    assert stored["processing_status"] == "pending"
    assert stored["ai_status"] == "waiting"
    assert stored["message_link"] is None  # Existing crawler links text-only posts by source and ID.

    second = await crawler.crawl_message(client, "source_channel", 567)
    assert second["saved"] == 0
    assert second["duplicates_skipped"] == 1
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_single_message_returns_explicit_not_found(manual_crawl_db):
    client = SingleMessageClient(None)
    result = await crawler.crawl_message(client, "source_channel", 1245)
    assert result["status"] == "not_found"
    assert result["saved"] == 0
    assert client.get_messages_calls == [1245]


@pytest.mark.asyncio
async def test_manual_operation_rejects_unknown_sources_and_accounts(monkeypatch):
    class FakeChannels:
        def load(self):
            return {"jobs": [{"username": "source_channel"}]}

    class FakeAccounts:
        async def list_accounts(self):
            return [{"session": "primary"}]

    operations = BackofficeOperations()
    operations._channels = FakeChannels()
    operations._accounts = FakeAccounts()
    with pytest.raises(ValueError, match="configured source"):
        await operations.crawl_single_message(
            account_name="primary", channel_username="unlisted_channel", message_id=23
        )
    with pytest.raises(ValueError, match="authorized Telegram"):
        await operations.crawl_single_message(
            account_name="unknown", channel_username="source_channel", message_id=23
        )
