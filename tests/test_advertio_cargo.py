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


def test_active_cargo_report_shows_all_potentially_active_but_blocks_unsupported(env):
    from backoffice_active_cargo import active_cargo_report
    service, client = env
    add_post(6001, departure=(date.today() + timedelta(days=3)).isoformat())
    add_post(6002, departure=(date.today() - timedelta(days=2)).isoformat())
    add_post(6003, overrides={"origin_country": "CN"})
    add_post(6004, age_days=90)
    add_post(6005)
    report = active_cargo_report()
    assert report["summary"]["total"] == 3
    assert report["summary"]["ready"] == 2
    assert report["total"] == 3
    assert {row["message_id"] for row in report["rows"]} == {6001, 6003, 6005}
    assert not next(row for row in report["rows"] if row["message_id"] == 6003)["sendable"]
    assert next(row for row in report["rows"] if row["message_id"] == 6003)["blocked_reason"]
    assert [row["message_id"] for row in report["rows"] if row["sendable"]] == [6001, 6005]


def test_active_cargo_report_search_trip_and_api_status_filters(env):
    from backoffice_active_cargo import active_cargo_report
    service, client = env
    add_post(6101, channel="different", departure=(date.today()+timedelta(days=1)).isoformat())
    add_post(6102, channel="another")
    assert active_cargo_report(trip="upcoming")["total"] == 1
    assert active_cargo_report(trip="undated")["total"] == 1
    assert active_cargo_report(search="different")["total"] == 1
    assert active_cargo_report(search="6102")["total"] == 1
    assert active_cargo_report(status="ready")["total"] == 2
    with pytest.raises(ValueError, match="filter"):
        active_cargo_report(status="sent' OR 1=1")
    with pytest.raises(ValueError, match="Search"):
        active_cargo_report(search="x"*101)


def test_selection_sends_only_chosen_ready_message_without_resend(env):
    service, client = env
    selected_row_id = add_post(6201, departure=(date.today()+timedelta(days=2)).isoformat())
    add_post(6202, departure=(date.today()+timedelta(days=3)).isoformat())
    expired_id = add_post(6203, departure=(date.today()-timedelta(days=1)).isoformat())
    stats = service.deliver_pending(row_ids=[selected_row_id, expired_id], limit=2)
    assert stats["found"] == 1
    assert stats["sent"] == 1
    assert [item["externalId"] for item in client.sent] == ["6201"]
    stats = service.deliver_pending(row_ids=[selected_row_id], limit=1)
    assert stats["found"] == 0
    assert len(client.sent) == 1
    with pytest.raises(ValueError):
        service.pending(row_ids=[-10])


@pytest.mark.asyncio
async def test_selected_cargo_delivery_controller_is_audited_and_independent(env, monkeypatch):
    from backoffice_operations import BackofficeOperations
    import asyncio
    from types import SimpleNamespace
    service, client = env
    row_id = add_post(6301)
    add_post(6302)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_CARGO_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_KEY", "test-key")
    monkeypatch.setattr("delivery.advertio_cargo.AdvertioCargoService", lambda: service)
    monkeypatch.setattr("backoffice_operations.database.record_system_activity", lambda *a, **kw: None)
    ops = BackofficeOperations(console_ui=None)
    ops._crawler = SimpleNamespace(scheduler=None)
    ops.start_selected_advertio_cargo([row_id], requested_by=7)
    await ops._tasks["advertio_cargo"]
    assert ops.state("advertio_cargo")["status"] == "completed"
    assert ops.state("advertio_cargo")["result"]["sent"] == 1
    assert [x["externalId"] for x in client.sent] == ["6301"]
    with pytest.raises(ValueError, match="Select between"):
        ops.start_selected_advertio_cargo([])
    with pytest.raises(ValueError, match="Select between"):
        ops.start_selected_advertio_cargo(range(1, 30))


def test_telegram_transferlive_view_uses_exact_same_deduplicated_cohort(env):
    from backoffice_active_cargo import active_cargo_report
    from monitoring import transfer_live

    tomorrow = (date.today() + timedelta(days=10)).isoformat()
    add_post(7101, departure=tomorrow)
    add_post(7102, departure=tomorrow)  # same sender, route and date
    add_post(7103, departure=tomorrow, overrides={"destination_city": "Ottawa"})
    add_post(7104, departure=tomorrow, overrides={"origin_country": "ZZ"})
    add_post(7105, departure=tomorrow, overrides={"destination_city": None})
    add_post(7106)  # fresh undated: Advertio candidate, NOT /transferlive
    add_post(7107, departure=(date.today() - timedelta(days=2)).isoformat())
    add_post(7108, departure=tomorrow,
             overrides={"destination_country": "RU", "destination_city": "Moscow"})

    telegram_ids = transfer_live.active_transferlive_message_ids()
    telegram_report = active_cargo_report(view="telegram")
    advertio_report = active_cargo_report()
    assert len(telegram_ids) == 4
    assert telegram_report["total"] == len(telegram_ids)
    assert telegram_report["summary"]["transferlive"] == len(telegram_ids)
    assert advertio_report["summary"]["transferlive"] == len(telegram_ids)
    assert advertio_report["total"] == 7
    assert {row["row_id"] for row in telegram_report["rows"]} == set(telegram_ids)
    assert telegram_report["summary"]["ready"] == 2
    assert telegram_report["filters"]["view"] == "telegram"
    assert {row["message_id"] for row in telegram_report["rows"]} == {7101, 7103, 7105, 7108}
    assert active_cargo_report(view="telegram", trip="undated")["total"] == 0
    with pytest.raises(ValueError, match="filter"):
        active_cargo_report(view="invalid")


def test_telegram_transferlive_view_empty_is_safe(env):
    from backoffice_active_cargo import active_cargo_report
    add_post(7110)  # undated: no Telegram /transferlive record
    report = active_cargo_report(view="telegram")
    assert report["summary"]["transferlive"] == 0
    assert report["total"] == 0
    assert report["rows"] == []


def _cargo_clock(monkeypatch):
    clock = [datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr('delivery.advertio_cargo.datetime', Clock)
    return clock


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('already_existed', [False, True])
def test_fast_restart_cargo_recovers_on_later_cycle_without_second_restart(env, monkeypatch, scheduled, already_existed):
    service, client = env
    clock = _cargo_clock(monkeypatch)
    row_id = add_post(8801, departure='2099-01-01')
    original_payload = service.build_payload(service.pending()[0])
    assert service._claim(row_id)  # Worker crashes before recording a result.
    restarted = AdvertioCargoService(client=client)
    assert restarted.deliver_pending()['found'] == 0
    assert not client.sent
    clock[0] += timedelta(minutes=31)
    if already_existed:
        def accept_existing(payload):
            client.sent.append(payload)
            return {'lead_id': 'existing-lead', 'already_existed': True}
        monkeypatch.setattr(client, 'create_lead', accept_existing)
    kwargs = {'before_datetime': (clock[0] + timedelta(seconds=1)).isoformat()} if scheduled else {'row_ids': [row_id]}
    result = restarted.deliver_pending(**kwargs)
    status = 'already_existed' if already_existed else 'sent'
    assert result[status] == 1
    assert client.sent == [original_payload]  # Identity is unchanged on recovery.
    assert restarted.deliver_pending(**kwargs)['found'] == 0


def test_cargo_recovery_preserves_active_and_terminal_records(env, monkeypatch):
    service, client = env
    clock = _cargo_clock(monkeypatch)
    snapshots = enumerate(['sending', 'sent', 'already_existed', 'rejected', 'retry'], 8900)
    for i, status in snapshots:
        row_id = add_post(i, departure='2099-01-01')
        stamp = clock[0] if status == 'sending' else clock[0] - timedelta(hours=2)
        conn = database.get_connection()
        conn.execute('''INSERT INTO advertio_cargo_deliveries
            (processed_message_id,status,updated_at,lead_id,attempts,next_attempt_at)
            VALUES(?,?,?,?,?,?)''', (row_id, status, stamp.isoformat(), 'preserve-me', 3,
                                  (clock[0] + timedelta(hours=2)).isoformat()))
        conn.commit(); conn.close()
    conn = database.get_connection()
    before = [tuple(r) for r in conn.execute('SELECT * FROM advertio_cargo_deliveries ORDER BY processed_message_id')]
    conn.close()
    service.deliver_pending()
    conn = database.get_connection()
    after = [tuple(r) for r in conn.execute('SELECT * FROM advertio_cargo_deliveries ORDER BY processed_message_id')]
    conn.close()
    assert after == before and not client.sent


def test_cargo_two_workers_recover_and_claim_only_once(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    service, client = env
    clock = _cargo_clock(monkeypatch)
    row_id = add_post(8991, departure='2099-01-01')
    assert service._claim(row_id)
    other = AdvertioCargoService(client=client)
    clock[0] += timedelta(minutes=31)
    barrier = Barrier(2)
    for worker in (service, other):
        claim = worker._claim
        def synchronized_claim(message_id, claim=claim):
            barrier.wait(timeout=5)
            return claim(message_id)
        monkeypatch.setattr(worker, '_claim', synchronized_claim)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(worker.deliver_pending) for worker in (service, other)]
        results = [job.result(timeout=10) for job in jobs]
    assert sum(r['sent'] for r in results) == 1
    assert len(client.sent) == 1


def test_cargo_recovery_allows_long_configured_request_timeout(env, monkeypatch):
    service, client = env
    clock = _cargo_clock(monkeypatch)
    # A cached client's longer timeout remains relevant after config changes.
    client.timeout = 3600
    monkeypatch.setattr(config, 'ADVERTIO_TIMEOUT_SECONDS', 60)
    row_id = add_post(8992, departure='2099-01-01')
    assert service._claim(row_id)
    clock[0] += timedelta(minutes=31)
    assert service.deliver_pending()['found'] == 0
    assert not client.sent
    clock[0] += timedelta(hours=3)
    assert service.deliver_pending()['sent'] == 1


@pytest.mark.parametrize('blocked_by', ['pause', 'budget'])
def test_cargo_recovery_does_not_bypass_pause_or_budget(env, monkeypatch, blocked_by):
    service, client = env
    clock = _cargo_clock(monkeypatch)
    row_id = add_post(8993, departure='2099-01-01')
    assert service._claim(row_id)
    clock[0] += timedelta(minutes=31)
    if blocked_by == 'pause':
        service._pause(3600)
    else:
        monkeypatch.setattr(config, 'ADVERTIO_CARGO_DAILY_LIMIT', 1)
        service._record_api_attempt(row_id)
    service.deliver_pending()
    conn = database.get_connection()
    row = conn.execute('SELECT status,next_attempt_at FROM advertio_cargo_deliveries WHERE processed_message_id=?', (row_id,)).fetchone()
    conn.close()
    assert tuple(row) == ('retry', None)
    assert not client.sent


def test_forward_first_username_overrides_later_ai_cargo_contact(env):
    service, _ = env
    add_post(9971, departure='2099-01-01')
    record = service.pending()[0]
    record.update(sender_type='admin_forward_text', sender_username='Helpero_ltd', contact='@karyabi_canada')
    payload = service.build_payload(record)
    assert payload['contactHandle'] == '@Helpero_ltd'
    assert 'posterTelegramUserId' not in payload
