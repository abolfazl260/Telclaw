"""Delivery compatibility contracts for raw, cleaned, and legacy message text.

All records are stored in temporary SQLite databases. Network calls are replaced
by explicit local fakes; no Telegram or Advertio delivery can occur.
"""

import json

import pytest

import config
import routed_publisher
from delivery.advertio_service import AdvertioDeliveryService
from delivery.telegram_transfer_publisher import (
    TelegramTransferPublisher,
    TransferTelegramPublishError,
)
from storage import database
from storage.message_repository import MessageRepository


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "delivery-text-compat.sqlite3"))
    database.initialize_db()
    return tmp_path


def seed_message(message_id, category, *, raw_text=None, text=None, cleaned_text=None, **state):
    assert database.insert_message(
        "source", message_id, text, "2026-09-29",
        raw_text=raw_text, cleaned_text=cleaned_text,
        message_link=f"https://t.me/source/{message_id}",
        sender_username="sourceuser",
    )
    database.update_message(
        message_id, "source",
        processing_status="processed", ai_status="processed",
        ai_category=category, **state
    )
    with database.get_connection() as conn:
        return conn.execute(
            "SELECT id FROM messages WHERE channel_username='source' AND message_id=?",
            (message_id,),
        ).fetchone()["id"]


def seed_transfer(message_id, *, raw_text=None, text=None, cleaned_text=None, **fields):
    row_id = seed_message(
        message_id, "transferlist", raw_text=raw_text,
        text=text, cleaned_text=cleaned_text
    )
    transfer = {
        "title": "Send a package from Tehran to Toronto",
        "description": "Small luggage available",
        "origin_city": "Tehran",
        "origin_country": "IR",
        "destination_city": "Toronto",
        "destination_country": "CA",
        "cargo_type": "parcel",
        "weight": 4,
        "weight_unit": "kg",
    }
    transfer.update(fields)
    database.save_category_record(row_id, "transferlist", transfer)
    return row_id


def seed_housing(message_id, *, raw_text=None, text=None, cleaned_text=None, status="waiting"):
    row_id = seed_message(
        message_id, "housinglist", raw_text=raw_text, text=text,
        cleaned_text=cleaned_text, advertio_status=status
    )
    database.save_category_record(row_id, "housinglist", {
        "listing_type": "rent",
        "property_type": "apartment",
        "title": "Apartment in Toronto",
        "description": "Available near downtown",
        "bedrooms": "2",
        "price": 2100,
        "currency": "CAD",
        "country_code": "CA",
        "province": "Ontario",
        "city": "Toronto",
    })
    return row_id


@pytest.mark.parametrize(
    "message_fields",
    [
        {"raw_text": "ORIGINAL\n  spaced  🧳", "text": "processed content", "cleaned_text": "processed content"},
        {"raw_text": None, "text": "legacy-only \n text", "cleaned_text": None},
        {"raw_text": "original only", "text": None, "cleaned_text": None},
        {"raw_text": None, "text": None, "cleaned_text": ""},
    ],
)
def test_transfer_sql_retains_legacy_columns_and_formats_structured_ad(db, message_fields):
    publisher = TelegramTransferPublisher(token="fake-token", channel="@fixture")
    seed_transfer(100, **message_fields)
    rows = publisher._pending_records()

    assert len(rows) == 1
    record = rows[0]
    assert record["raw_text"] == message_fields["raw_text"]
    assert record["text"] == message_fields["text"]
    assert "cleaned_text" not in record  # Legacy SQL shape is intentional.
    formatted = publisher.format_ad(record, record)
    assert "Send a package from Tehran to Toronto" in formatted
    assert "Origin: Tehran" in formatted
    assert "Destination: Toronto" in formatted
    assert "📝 Description: Small luggage available" in formatted
    raw = message_fields["raw_text"]
    if raw and raw != "Small luggage available":
        assert f"📝 Description: {raw}" not in formatted


@pytest.mark.asyncio
async def test_transfer_success_is_not_resent_when_legacy_text_changes(db, monkeypatch):
    publisher = TelegramTransferPublisher(token="fake-token", channel="@fixture")
    row_id = seed_transfer(101, raw_text="unchanged source", text="old legacy")
    sent = []

    async def fake_send(payload, markup=None):
        sent.append((payload, markup))
        return {"message_id": 90001}

    monkeypatch.setattr(publisher, "_send_message", fake_send)
    first = await publisher.publish_pending()
    assert first == {"found": 1, "sent": 1, "failed": 0, "rejected": 0}
    assert len(sent) == 1

    with database.get_connection() as conn:
        existing = conn.execute(
            "SELECT ad_number, status, telegram_message_id "
            "FROM telegram_transfer_publications WHERE message_row_id=?",
            (row_id,),
        ).fetchone()
        ad_number = existing["ad_number"]
        assert existing["status"] == "sent"
        assert existing["telegram_message_id"] == 90001

    assert ad_number == 7240
    assert database.update_message(101, "source", text="independently edited legacy text")
    second = await publisher.publish_pending()
    assert second == {"found": 0, "sent": 0, "failed": 0, "rejected": 0}
    assert len(sent) == 1
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT ad_number, status FROM telegram_transfer_publications "
            "WHERE message_row_id=?", (row_id,)
        ).fetchone()
    assert (row["ad_number"], row["status"]) == (ad_number, "sent")


@pytest.mark.asyncio
async def test_transfer_retry_keeps_ad_number_and_rejected_is_not_requeued(db, monkeypatch):
    publisher = TelegramTransferPublisher(token="fake-token", channel="@fixture")
    retry_id = seed_transfer(201, raw_text=None, text="legacy-only record")
    rejected_id = seed_transfer(
        202, raw_text="original", cleaned_text="cleaned", destination_city=None
    )
    attempts = []

    async def initially_fail(payload, markup=None):
        attempts.append(payload)
        raise TransferTelegramPublishError("Temporary Telegram failure")

    monkeypatch.setattr(publisher, "_send_message", initially_fail)
    first = await publisher.publish_pending()
    assert first == {"found": 2, "sent": 0, "failed": 1, "rejected": 1}
    assert len(attempts) == 1  # Missing route is rejected BEFORE Telegram send.

    with database.get_connection() as conn:
        statuses = {
            row["message_row_id"]: (row["status"], row["ad_number"])
            for row in conn.execute(
                "SELECT message_row_id, status, ad_number FROM telegram_transfer_publications"
            )
        }
    assert statuses[retry_id][0] == "retry"
    assert statuses[rejected_id][0] == "rejected"
    ad_number = statuses[retry_id][1]

    async def succeed(payload, markup=None):
        attempts.append(payload)
        return {"message_id": 90002}

    monkeypatch.setattr(publisher, "_send_message", succeed)
    second = await publisher.publish_pending()
    assert second == {"found": 1, "sent": 1, "failed": 0, "rejected": 0}
    assert len(attempts) == 2
    assert f"TR-{ad_number:06d}" in attempts[0]
    assert f"TR-{ad_number:06d}" in attempts[1]
    assert (await publisher.publish_pending())["found"] == 0

    with database.get_connection() as conn:
        statuses_after = {
            row["message_row_id"]: (row["status"], row["ad_number"])
            for row in conn.execute(
                "SELECT message_row_id, status, ad_number FROM telegram_transfer_publications"
            )
        }
    assert statuses_after[retry_id] == ("sent", ad_number)
    assert statuses_after[rejected_id] == statuses[rejected_id]


@pytest.mark.parametrize(
    "record,data,expected",
    [
        ({"raw_text": "Apartment available nightly", "text": None}, {}, "daily"),
        ({"raw_text": None, "text": "weekly sublet available"}, {}, "short_term"),
        ({"raw_text": None, "text": None, "cleaned_text": "weekly"}, {}, "long_term"),
        ({"raw_text": None, "text": None}, {}, "long_term"),
        ({"raw_text": "nightly rental"}, {"rent_period": "long_term"}, "long_term"),
        ({"raw_text": "weekly rental"}, {"description": "daily accommodation"}, "daily"),
        ({"raw_text": "روزانه سوییت", "text": None}, {}, "daily"),
        ({"raw_text": None, "text": "اجاره هفتگی"}, {}, "short_term"),
    ],
)
def test_advertio_rent_duration_keeps_structured_and_legacy_text_rules(record, data, expected):
    assert AdvertioDeliveryService._infer_rental_duration(data, record) == expected


def test_advertio_replays_neither_sent_nor_rejected_housing_records(db):
    seed_housing(301, raw_text=None, text="weekly rental", status="waiting")
    seed_housing(302, raw_text="old raw", text="old text", status="sent")
    seed_housing(303, raw_text=None, text=None, status="rejected")
    calls = []

    class FakeClient:
        def create_lead(self, payload):
            calls.append(payload)
            return {"lead_id": "lead-test", "already_existed": False}

        def upload_media(self, *_args):
            raise AssertionError("No media uploads should occur")

    service = AdvertioDeliveryService(
        client=FakeClient(), repository=MessageRepository()
    )
    first = service.deliver_pending(progress=False)
    assert first == {"found": 1, "sent": 1, "already_existed": 0, "failed": 0}
    assert len(calls) == 1
    assert calls[0]["externalId"] == "301"
    assert json.loads(calls[0]["attributesJson"])["rental_duration"] == "short_term"

    second = service.deliver_pending(progress=False)
    assert second == {"found": 0, "sent": 0, "already_existed": 0, "failed": 0}
    assert len(calls) == 1

    with database.get_connection() as conn:
        rows = {
            row["message_id"]: dict(row)
            for row in conn.execute(
                "SELECT message_id, raw_text, text, advertio_status, advertio_lead_id FROM messages"
            )
        }
    assert rows[301]["raw_text"] is None
    assert rows[301]["text"] == "weekly rental"
    assert rows[301]["advertio_status"] == "sent"
    assert rows[301]["advertio_lead_id"] == "lead-test"
    assert rows[302]["advertio_status"] == "sent"
    assert rows[303]["advertio_status"] == "rejected"


def test_routed_non_transfer_preview_preserves_structured_and_raw_fallbacks(monkeypatch):
    monkeypatch.setattr(routed_publisher.routing_rules, "categories", lambda: ("joblist",))
    monkeypatch.setattr(
        routed_publisher.routing_rules,
        "category_fields",
        lambda category: ("job_title", "description"),
    )

    base = {"ai_category": "joblist", "job_title": None, "description": None}
    assert routed_publisher._plain_ad({
        **base, "job_title": "Warehouse assistant", "description": "Apply in person",
        "cleaned_text": "cleaned payload", "raw_text": "raw payload",
        "text": "legacy payload",
    }) == "Warehouse assistant\nApply in person"
    assert routed_publisher._plain_ad({
        **base, "cleaned_text": "  Cleaned fallback ✓  ",
        "raw_text": "raw payload", "text": "legacy payload",
    }) == "Cleaned fallback ✓"
    assert routed_publisher._plain_ad({
        **base, "cleaned_text": None, "raw_text": "  Raw fallback 🧳 ",
        "text": "legacy payload",
    }) == "Raw fallback 🧳"
    # The routed preview deliberately does not use the legacy compatibility copy.
    assert routed_publisher._plain_ad({
        **base, "cleaned_text": None, "raw_text": None, "text": "legacy payload",
    }) == ""
