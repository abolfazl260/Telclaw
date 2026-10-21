"""Contract and queue regressions for cargo->Advertio; no live API access."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import config
from delivery.advertio_cargo import AdvertioCargoService, CargoMappingError, source_name
from delivery.advertio_client import AdvertioError
from storage import database


class FakeClient:
    def __init__(self):
        self.sent = []
        self.failure = None

    def list_cities(self, country):
        cities = {
            "CA": [dict(name="Toronto", nameFa="تورنتو", code="toronto", countryCode="CA", provinceCode="ON")],
            "IR": [dict(name="Tehran", nameFa="تهران", code="tehran", countryCode="IR", provinceCode="TEHRAN")],
            "US": [dict(name="Dallas", nameFa="دالاس", code="dallas", countryCode="US", provinceCode="TX")],
        }
        return cities.get(country, [])

    def create_lead(self, payload):
        self.sent.append(payload)
        if self.failure:
            raise self.failure
        return {"lead_id": "lead-" + payload["externalId"], "already_existed": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "cargo.sqlite"))
    monkeypatch.setattr(config, "ADVERTIO_CARGO_BACKFILL_DAYS", 30)
    monkeypatch.setattr(config, "ADVERTIO_CARGO_AUTO_PUBLISH", False)
    monkeypatch.setattr(config, "ADVERTIO_CARGO_BATCH_SIZE", 50)
    monkeypatch.setattr(config, "ADVERTIO_CARGO_DAILY_LIMIT", 250)
    database.initialize_db()
    fake = FakeClient()
    return AdvertioCargoService(client=fake), fake


def add_post(message_id, *, channel="cargo-one", age_days=3, departure=None, overrides=None):
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).date().isoformat()
    database.insert_message(
        channel, message_id, "Toronto Tehran parcel offer", created,
        raw_text="از تورنتو به تهران کیلویی ۱۵ دلار کانادا",
        message_link="https://t.me/" + channel + "/" + str(message_id),
        sender_id=987654321, sender_type="user", sender_username="real_sender",
    )
    database.update_message(
        message_id, channel,
        processing_status="processed", ai_status="processed", ai_category="transferlist",
    )
    conn = database.get_connection()
    try:
        row_id = conn.execute(
            "SELECT id FROM messages WHERE channel_username=? AND message_id=?",
            (channel, message_id),
        ).fetchone()[0]
    finally:
        conn.close()
    data = {
        "origin_city": "Toronto", "origin_country": "CA",
        "destination_city": "Tehran", "destination_country": "IR",
        "departure_date": departure,
        "cargo_type": '["documents","electronics"]',
        "weight": 10, "weight_unit": "kg",
        "price": 15, "currency": "CAD",
        "description": "از تورنتو به تهران کیلویی ۱۵ دلار کانادا",
    }
    data.update(overrides or {})
    database.save_category_record(row_id, "transferlist", data)
    return row_id


def test_cargo_contract_and_independent_housing_status(env):
    service, client = env
    add_post(1001, departure=(date.today() + timedelta(days=5)).isoformat())
    stats = service.deliver_pending()
    assert stats["sent"] == 1
    assert len(client.sent) == 1
    payload = client.sent[0]
    assert payload["categorySlug"] == "passenger-cargo"
    assert payload["mediaKeys"] == []
    assert payload["autoPublish"] is False
    assert payload["posterTelegramUserId"] == 987654321
    assert payload["contactHandle"] == "@real_sender"
    assert payload["province"] == "ON"
    attrs = json.loads(payload["attributesJson"])
    assert attrs["origin"] == "Toronto" and attrs["destination"] == "Tehran"
    assert attrs["weight_kg"] == 10
    assert attrs["cargo_type"] == ["documents", "electronics"]
    assert attrs["price"] == 15 and attrs["currency"] == "CAD"
    assert "role" not in attrs
    assert service.deliver_pending()["found"] == 0
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT advertio_status FROM messages WHERE message_id=1001").fetchone()[0] == "waiting"
    finally:
        conn.close()


def test_backfill_is_fresh_or_future_dated_only(env):
    service, client = env
    add_post(1, age_days=4)  # recent undated listing
    add_post(2, age_days=90)  # stale undated listing
    add_post(3, age_days=90, departure=(date.today() + timedelta(days=5)).isoformat())
    add_post(4, age_days=4, departure=(date.today() - timedelta(days=1)).isoformat())
    stats = service.deliver_pending()
    assert stats["sent"] == 2
    assert stats["rejected"] == 1
    assert {x["externalId"] for x in client.sent} == {"1", "3"}


def test_identical_telegram_message_ids_from_different_channels(env):
    service, client = env
    add_post(27, channel="cargo-one")
    add_post(27, channel="cargo-two")
    result = service.deliver_pending()
    assert result["sent"] == 2
    assert client.sent[0]["sourceName"] != client.sent[1]["sourceName"]
    assert client.sent[0]["externalId"] == client.sent[1]["externalId"] == "27"


def test_undated_listing_does_not_invent_a_date(env):
    service, client = env
    add_post(32, departure=None)
    service.deliver_pending()
    attributes = json.loads(client.sent[0]["attributesJson"])
    assert "travel_date" not in attributes
    assert client.sent[0]["title"] == "Toronto → Tehran"


def test_unknown_city_is_not_replaced_with_nearest_city(env):
    service, client = env
    add_post(38, overrides={"destination_city": "UnlistedCity"})
    stats = service.deliver_pending()
    assert stats["rejected"] == 1
    assert client.sent == []


def test_429_is_retry_and_does_not_affect_telegram_state(env):
    service, client = env
    add_post(55)
    client.failure = AdvertioError("rate limit", status=429, retryable=True)
    stats = service.deliver_pending()
    assert stats["retry"] == 1
    assert service.deliver_pending()["found"] == 0  # backoff
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT status FROM advertio_cargo_deliveries").fetchone()[0] == "retry"
        assert conn.execute("SELECT advertio_status FROM messages WHERE message_id=55").fetchone()[0] == "waiting"
    finally:
        conn.close()


def test_only_explicit_per_kg_price_is_structured(env):
    service, client = env
    add_post(71, overrides={"price": 30, "currency": "CAD",
                             "description": "Flat fee 30 CAD"})
    conn = database.get_connection()
    try:
        conn.execute("UPDATE messages SET raw_text=? WHERE message_id=71",
                     ("Flat fee 30 CAD",))
        conn.commit()
    finally:
        conn.close()
    service.deliver_pending()
    attrs = json.loads(client.sent[0]["attributesJson"])
    assert "price" not in attrs and "currency" not in attrs


def test_non_user_sender_must_not_be_claimed_as_author(env):
    service, client = env
    add_post(91)
    conn = database.get_connection()
    try:
        conn.execute("UPDATE messages SET sender_type='channel' WHERE message_id=91")
        conn.commit()
    finally:
        conn.close()
    service.deliver_pending()
    assert client.sent[0].get("posterTelegramUserId") is None
    assert client.sent[0]["contactHandle"] is None
    assert client.sent[0]["sourceUrl"]


def test_budget_and_already_existed(env, monkeypatch):
    service, client = env
    monkeypatch.setattr(config, "ADVERTIO_CARGO_DAILY_LIMIT", 1)
    add_post(201)
    add_post(202)
    original = client.create_lead
    def already(payload):
        client.sent.append(payload)
        return {"lead_id": "existing", "already_existed": True}
    client.create_lead = already
    stats = service.deliver_pending()
    assert stats["already_existed"] == 1
    assert stats["budget_remaining"] == 0
    assert service.deliver_pending()["found"] == 0
