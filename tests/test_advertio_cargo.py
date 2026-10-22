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
    assert stats["found"] == 2
    assert stats["sent"] == 2
    # Already-expired trips are never selected and therefore do not produce
    # synthetic rejection rows or API attempts during historical backfill.
    assert stats["rejected"] == 0
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


def test_rate_limit_pauses_other_waiting_records(env):
    service, client = env
    add_post(311)
    add_post(312)
    client.failure = AdvertioError("over limit", status=429, retryable=True)
    first = service.deliver_pending()
    assert first["retry"] == 1
    assert first["paused_until"]
    assert len(client.sent) == 1
    client.failure = None
    second = service.deliver_pending()
    assert second["found"] == 0
    assert second["paused_until"]
    assert len(client.sent) == 1


def test_source_name_is_stable_under_username_change_when_channel_id_exists():
    first = {"channel_username": "old_name", "channel_id": 123456}
    renamed = {"channel_username": "new_name", "channel_id": 123456}
    other = {"channel_username": "new_name", "channel_id": 999999}
    assert source_name(first) == source_name(renamed)
    assert source_name(first) != source_name(other)


def test_backoffice_cargo_report_shows_unattempted_and_completed_posts(env):
    from backoffice_health import _cargo_delivery_report
    service, client = env
    add_post(801)
    add_post(802)
    conn = database.get_connection()
    try:
        before = _cargo_delivery_report(conn)
    finally:
        conn.close()
    assert before["processed"] == 2
    assert before["waiting"] == 2
    assert before["ready_to_send"] == 2
    assert before["attempts_today"] == 0
    assert before["recent"][0]["message_id"] == 802
    service.deliver_pending(limit=1)
    conn = database.get_connection()
    try:
        after = _cargo_delivery_report(conn)
    finally:
        conn.close()
    assert after["sent"] == 1
    assert after["waiting"] == 1
    assert after["ready_to_send"] == 1
    assert after["attempts_today"] == 1
    # The worker now prioritizes the newest undated record, not the oldest ID.
    assert after["recent"][0]["status"] == "sent"
    assert all("row_id" in row for row in after["recent"])


def test_backoffice_cargo_report_explains_expired_and_stale(env):
    from backoffice_health import _cargo_delivery_report
    service, client = env
    add_post(901, age_days=91)
    add_post(902, departure=(date.today() - timedelta(days=2)).isoformat())
    conn = database.get_connection()
    try:
        report = _cargo_delivery_report(conn)
    finally:
        conn.close()
    assert report["processed"] == 2
    assert report["waiting"] == 0
    assert report["expired"] == 1
    assert report["stale"] == 1


def test_cargo_requeue_rejected_is_targeted_and_auditable(env, monkeypatch):
    from backoffice_operations import BackofficeOperations
    service, client = env
    row_id = add_post(903, overrides={"destination_city": "unlisted"})
    assert service.deliver_pending()["rejected"] == 1
    monkeypatch.setattr("backoffice_operations.database.record_system_activity", lambda *a, **k: None)
    controller = BackofficeOperations(console_ui=None)
    assert controller.requeue_rejected_cargo(row_id) == row_id
    conn = database.get_connection()
    try:
        assert conn.execute(
            "SELECT status FROM advertio_cargo_deliveries WHERE processed_message_id=?",
            (row_id,),
        ).fetchone()[0] == "waiting"
    finally:
        conn.close()
    with pytest.raises(ValueError, match="not a rejected"):
        controller.requeue_rejected_cargo(row_id)


def test_new_unique_cargo_is_submitted_for_auto_publish(env, monkeypatch):
    service, client = env
    monkeypatch.setattr(config, "ADVERTIO_CARGO_AUTO_PUBLISH", True)
    add_post(8808)
    stats = service.deliver_pending(limit=1)
    assert stats["sent"] == 1
    assert client.sent[0]["autoPublish"] is True
    assert client.sent[0]["categorySlug"] == "passenger-cargo"


def test_queue_skips_recently_recrawled_expired_and_unsupported_routes(env):
    service, client = env
    add_post(5001, age_days=1, departure=(date.today() - timedelta(days=20)).isoformat())
    add_post(5002, age_days=1, overrides={"origin_country": "CN"})
    add_post(5003, age_days=1, overrides={"destination_city": None})
    add_post(5004, age_days=1, departure=(date.today() + timedelta(days=2)).isoformat())

    records = service.pending(limit=10)
    assert [record["message_id"] for record in records] == [5004]
    stats = service.deliver_pending(limit=10)
    assert stats["found"] == 1
    assert stats["sent"] == 1
    assert stats["rejected"] == 0
    assert [item["externalId"] for item in client.sent] == ["5004"]


def test_nearest_departures_are_prioritized_before_undated_posts(env):
    service, client = env
    add_post(5101, departure=(date.today() + timedelta(days=30)).isoformat())
    add_post(5102, departure=(date.today() + timedelta(days=2)).isoformat())
    add_post(5103)
    add_post(5104, departure=(date.today() + timedelta(days=7)).isoformat())

    ordered = [record["message_id"] for record in service.pending(limit=4)]
    assert ordered == [5102, 5104, 5101, 5103]
    stats = service.deliver_pending(limit=2)
    assert stats["sent"] == 2
    assert [item["externalId"] for item in client.sent] == ["5102", "5104"]


def test_dashboard_ready_count_matches_queue_not_broad_waiting(env):
    from backoffice_health import _cargo_delivery_report
    service, client = env
    add_post(5201, departure=(date.today() + timedelta(days=3)).isoformat())
    add_post(5202, departure=(date.today() - timedelta(days=2)).isoformat())
    add_post(5203, overrides={"origin_country": "CN"})
    add_post(5204, overrides={"origin_city": ""})
    conn = database.get_connection()
    try:
        report = _cargo_delivery_report(conn)
    finally:
        conn.close()
    assert report["ready_to_send"] == 1
    assert report["waiting"] >= 1
    assert len(service.pending(limit=50)) == report["ready_to_send"]
