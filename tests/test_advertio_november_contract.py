"""2026-11 Advertio contract regressions against SQLite and fake transports."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from threading import Event
from types import SimpleNamespace

import pytest

import config
from delivery import advertio_cargo_catalog as catalog, advertio_transport as transport
from delivery.advertio_cargo import AdvertioCargoService, _cargo_types, source_name
from delivery.advertio_cargo_review import review_history
from delivery.advertio_client import AdvertioClient, AdvertioError
from delivery.advertio_lifecycle import (
    create_listing, deletion_candidates, deliver_deletions, duplicate_report, record_source_check,
)
from delivery.advertio_source_text import has_cargo_price
from storage import database
from test_advertio_cargo import CONTRACT_COUNTRIES, FakeClient, add_post
from test_advertio_contract import _housing, _record, _service


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "contract.sqlite"))
    monkeypatch.setattr(config, "ADVERTIO_BASE_URL", "https://advertio.invalid")
    monkeypatch.setattr(transport, "PACE_SECONDS", 0)
    database.initialize_db()


def city(name, code, country, province, name_fa):
    return dict(name=name, code=code, countryCode=country, provinceCode=province, nameFa=name_fa)


def test_snapshot_restart_expansion_partial_and_catalog_only_parity(db, monkeypatch):
    client = FakeClient()
    client.extra_cities.update({
        "AT": [city("Vienna", "vienna", "AT", "VIENNA", "وین")],
        "DK": [city("Copenhagen", "copenhagen", "DK", "CAPITAL-REGION", "کپنهاگ")],
        "CA": [city("London", "london", "CA", "ON", "لندن")],
        "GB": [city("London", "london", "GB", "LONDON", "لندن")],
    })
    service = AdvertioCargoService(client)
    row = add_post(89, overrides={"origin_city": "وین", "origin_country": "Austria"})
    assert service.pending() == []
    snap = catalog.refresh(client, service.host)
    assert len(snap.data["countries"]) == len(CONTRACT_COUNTRIES) == 48
    assert client.city_calls == sorted(CONTRACT_COUNTRIES)
    assert catalog.refresh(client, service.host).synced_at == snap.synced_at
    assert len(client.city_calls) == 48
    assert AdvertioCargoService(client).pending()[0]["message_row_id"] == row
    from backoffice_active_cargo import active_cargo_report

    assert active_cargo_report(status="ready")["rows"][0]["row_id"] == row
    assert len(client.city_calls) == 48
    record = service.pending()[0]
    payload = service.build_payload(record, snapshot=snap)
    assert (payload["countryCode"], payload["province"], payload["city"]) == ("AT", "VIENNA", "vienna")
    assert service.build_payload({**record, "origin_country": "DK", "origin_city": "کپنهاگ"},
                                 snapshot=snap)["province"] == "CAPITAL-REGION"
    assert snap.resolve("origin", "CA", "London", "London to Tehran and Canada stop")[1] == "ambiguous_city"
    assert snap.resolve("origin", "CA", "London", "London, Ontario → Tehran")[0][1] == "london"
    assert snap.resolve("origin", "", "لندن", "لندن → تهران")[1] == "ambiguous_city"
    assert snap.resolve("origin", "QA", "Doha", "")[1] == "unsupported_city"

    clock = [datetime.fromisoformat(snap.synced_at) + timedelta(hours=25)]
    monkeypatch.setattr(catalog, "now", lambda: clock[0])
    client.extra_cities["AT"] = []
    assert catalog.refresh(client, service.host).synced_at == snap.synced_at
    with closing(database.get_connection()) as conn:
        assert catalog.metadata(conn)["status"] == "stale"
        assert catalog.metadata(conn)["last_error"] == "cargo_city_catalog_regressed:AT"
    with pytest.raises(AdvertioError, match="stale"):
        service.build_payload({**record, "origin_city": "Unsupported"}, snapshot=snap)
    clock[0] += timedelta(minutes=16)
    client.extra_cities["AT"] = [city("Vienna", "vienna", "AT", "VIENNA", "وین")]
    client.countries.append("NZ")
    client.extra_cities["NZ"] = [city("Auckland", "auckland", "NZ", "AUCKLAND", "اوکلند")]
    assert catalog.refresh(client, service.host).resolve("origin", "NZ", "Auckland", "")[0]


def test_refresh_coalesces_and_city_get_is_authenticated_per_country(db, monkeypatch):
    client = FakeClient()
    service = AdvertioCargoService(client)
    entered, release = Event(), Event()
    fetch = client.catalog

    def blocked(path):
        assert not entered.is_set() or path == "locations/countries"
        entered.set()
        assert release.wait(5)
        return fetch(path)

    client.catalog = blocked
    with ThreadPoolExecutor(max_workers=2) as workers:
        future = workers.submit(catalog.refresh, client, service.host)
        assert entered.wait(5)
        assert catalog.refresh(client, service.host) is None
        release.set()
        assert future.result(timeout=5)
    assert len(client.city_calls) == 48

    def get(url, **kwargs):
        assert url.endswith("/api/locations/cities")
        assert kwargs["params"] == {"countryCode": "DK", "limit": 200}
        assert kwargs["headers"] == {"X-Ingest-Key": "placeholder"}
        return SimpleNamespace(status_code=200, json=lambda: [])

    monkeypatch.setattr("delivery.advertio_client.requests.get", get)
    assert AdvertioClient(config.ADVERTIO_BASE_URL, "placeholder").list_cities("DK") == []


def test_historical_valid_waiting_routes_send_automatically_with_moderation(db, monkeypatch):
    row = add_post(100, overrides={"origin_country": "AT", "origin_city": "Vienna"})
    expired = add_post(101, age_days=90, overrides={"origin_country": "AT", "origin_city": "Vienna"})
    with closing(database.get_connection()) as conn, conn:
        conn.execute("UPDATE advertio_cargo_history_boundary SET max_message_id=?", (expired,))
    client = FakeClient()
    client.extra_cities["AT"] = [city("Vienna", "vienna", "AT", "VIENNA", "وین")]
    service = AdvertioCargoService(client)
    monkeypatch.setattr(config, "ADVERTIO_CARGO_AUTO_PUBLISH", True)
    monkeypatch.setattr(config, "ADVERTIO_CARGO_REVIEWED_SOURCES", {source_name({"channel_username": "cargo-one"})})
    catalog.refresh(client, service.host)
    assert [r["message_row_id"] for r in service.pending()] == [row]
    from backoffice_active_cargo import active_cargo_report
    from backoffice_health import _cargo_delivery_report
    assert active_cargo_report(status="ready")["total"] == 1
    with closing(database.get_connection()) as conn:
        assert _cargo_delivery_report(conn)["ready_to_send"] == 1
    assert review_history(limit=1)["rows"] == []
    assert review_history(apply_ids=(row, expired))["applied"] == []
    assert service.deliver_pending()["sent"] == 1
    assert client.sent[0]["autoPublish"] is False
    assert service.deliver_pending()["found"] == 0
    assert len(client.sent) == 1


@pytest.mark.parametrize("text,currency", (
    ("CAD 15/kg", "CAD"), ("15 USD per kg", "USD"), ("€15/kg", "EUR"),
    ("£15/kg", "GBP"), ("كيلویی ۱۵ درهم امارات".replace("ي", "ی"), "AED"),
    ("15 yuan per kg", "CNY"), ("15 ₺/kg", "TRY"), ("15 рублей за кг", "RUB"),
))
def test_cargo_supported_currencies(text, currency):
    assert has_cargo_price(text, 15, currency)


def test_cargo_types_and_unsubstantiated_quotes():
    assert _cargo_types('["food","مدارک","food","maybe"]') == ["food", "documents"]
    assert _cargo_types("food / documents، clothes") == ["food", "documents", "clothes"]
    for text in ("$15/kg", "15 CAD total", "AUD 15/kg", "15 CAD total; weight 5kg", "15 CAD total, 3 USD/kg"):
        assert not has_cargo_price(text, 15, "CAD")


@pytest.mark.parametrize("property_type,bedrooms", (("room", "1"), ("studio", "0"), ("apartment", "2"), ("house", "4+")))
def test_negotiable_housing_uses_original_evidence_and_no_amount(property_type, bedrooms):
    record = {**_record(), "raw_text": "اجاره توافقی در تورنتو"}
    payload = _service().build_payload(record, _housing(
        property_type=property_type, bedrooms=bedrooms, price=None, currency=None,
    ))
    attrs = json.loads(payload["attributesJson"])
    assert attrs["price_negotiable"] is True and "price" not in attrs
    assert payload["description"] == record["raw_text"]
    assert payload["autoPublish"] is False
    with pytest.raises(ValueError, match="currency"):
        _service().build_payload(record, _housing(price=2000, currency="USD"))
    assert "price_negotiable" not in json.loads(_service().build_payload(record, _housing())["attributesJson"])


def test_global_budget_cooldown_and_delete_404_distinction(db, monkeypatch):
    monkeypatch.setattr(config, "ADVERTIO_DAILY_REQUEST_LIMIT", 7)
    stamp = [datetime(2026, 11, 24, 16, tzinfo=timezone.utc)]
    monkeypatch.setattr(transport, "now", lambda: stamp[0])
    response = SimpleNamespace(status_code=429, headers={"Retry-After": "1200"})
    monkeypatch.setattr("delivery.advertio_client.requests.post", lambda *a, **kw: response)
    client = AdvertioClient(config.ADVERTIO_BASE_URL, "placeholder", category="housing")
    with pytest.raises(AdvertioError, match="HTTP 429"):
        client.create_lead({})
    for bucket in ("cargo", "jobs", "catalog", "deletion"):
        with pytest.raises(AdvertioError, match="upstream_http_429") as denied:
            with transport.request_slot(bucket):
                pytest.fail("shared cooldown was bypassed")
        assert denied.value.deferred and denied.value.retry_after == 1200
    stamp[0] += timedelta(minutes=21)
    response = SimpleNamespace(status_code=404, json=lambda: {"code": "lead_not_found"})
    monkeypatch.setattr("delivery.advertio_client.requests.delete", lambda *a, **kw: response)
    assert client.delete_lead("example-source", "20") == 404
    for bucket in ("cargo", "jobs", "catalog"):
        with transport.request_slot(bucket):
            pass
    response.json = lambda: {}
    with pytest.raises(AdvertioError) as unmapped:
        client.delete_lead("example-source", "21")
    assert unmapped.value.status == 404 and unmapped.value.retryable
    # A generic deletion 404 must not pause unrelated traffic.
    with transport.request_slot("housing"):
        pass
    with pytest.raises(AdvertioError, match="local_daily_budget"):
        with transport.request_slot("housing"):
            pytest.fail("requests exceeded the shared budget")
    with closing(database.get_connection()) as conn:
        assert transport.metadata(conn)["remaining"] == 0


def test_duplicate_first_send_deletion_and_original_key(db, monkeypatch):
    client = SimpleNamespace(
        create_lead=lambda payload: {"lead_id": "another-lead", "status": "PendingReview", "already_existed": True}
    )
    record = {"id": 7, "message_id": 71, "channel_id": 99, "channel_username": "source"}
    payload = {"sourceName": "original-source", "externalId": "71"}
    create_listing(client, payload, record, "cargo")
    create_listing(client, {**payload, "sourceName": "changed-source"}, record, "cargo")
    duplicates = duplicate_report()
    assert [(d["duplicate_first_send"], d["remote_status"]) for d in duplicates] == [(1, "PendingReview")]
    assert deletion_candidates(99) == [71]
    record_source_check(99, [], [71])
    record_source_check(99, [], [71])
    assert not deletion_candidates(99)
    assert deliver_deletions(client)["deleted"] == 0
    now = datetime.now(timezone.utc) + timedelta(hours=7)
    monkeypatch.setattr("delivery.advertio_lifecycle.now", lambda: now)
    record_source_check(99, [], [71])
    deletes = []
    client.delete_lead = lambda *key: (deletes.append(key) or 204)
    assert deliver_deletions(client)["deleted"] == 1
    assert deletes == [("original-source", "71")]
    assert deliver_deletions(client)["deleted"] == 0
    with pytest.raises(AdvertioError, match="removed"):
        create_listing(client, payload, record, "cargo")


def test_request_concurrency_applies_across_independent_connections(db, monkeypatch):
    monkeypatch.setattr(config, "ADVERTIO_CONCURRENCY", 1)
    with ThreadPoolExecutor(max_workers=1) as worker:
        with transport.request_slot("housing"):
            def request():
                with transport.request_slot("cargo"):
                    pass
            future = worker.submit(request)
            with pytest.raises(AdvertioError, match="local_concurrency_busy"):
                future.result(timeout=3)
        worker.submit(request).result(timeout=3)
    with closing(database.get_connection()) as conn:
        assert transport.metadata(conn)["by_category"] == {"cargo": 1, "housing": 1}


def test_cargo_json_types_keep_readable_telegram_display():
    from delivery.telegram_transfer import cargo_type_label

    assert cargo_type_label('["documents","food"]') == "documents, food"
    assert cargo_type_label("historical cargo") == "historical cargo"


def test_housing_reuses_each_uploaded_album_slot_after_deferral(db, tmp_path):
    from delivery.advertio_lifecycle import housing_media_key

    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"equal photo bytes")
    second.write_bytes(b"equal photo bytes")
    calls = []

    def upload(path, source):
        calls.append(path.name)
        return f"{source}-{path.name}"

    client = SimpleNamespace(upload_media=upload)
    for _ in range(2):
        assert [
            housing_media_key(client, path, "housing-test", "100", position)
            for position, path in enumerate((first, second))
        ] == ["housing-test-first.jpg", "housing-test-second.jpg"]
    assert calls == ["first.jpg", "second.jpg"]


@pytest.mark.asyncio
async def test_telegram_removal_requires_explicit_empty_ids_and_live_anchor(monkeypatch):
    types = pytest.importorskip("telethon.tl.types")
    from collection import advertio_source_removal as removal

    recorded = []
    monkeypatch.setattr(removal, "deletion_candidates", lambda _: [71])
    monkeypatch.setattr(removal, "record_source_check", lambda *args: recorded.append(args))
    channel = types.InputChannel(99, 1)
    anchor = SimpleNamespace(id=72, peer_id=types.PeerChannel(99))

    async def partial(_request):
        return SimpleNamespace(messages=[anchor])

    async def unavailable(_request):
        raise TimeoutError

    async def complete(request):
        assert [item.id for item in request.id] == [71, 72]
        return SimpleNamespace(messages=[types.MessageEmpty(71, types.PeerChannel(99)), anchor])

    await removal.check_source_removals(partial, channel, anchor.id)
    await removal.check_source_removals(unavailable, channel, anchor.id)
    assert not recorded
    await removal.check_source_removals(complete, channel, anchor.id)
    assert recorded == [(99, [], [71])]


def test_housing_retry_uploads_media_under_the_frozen_source(db, monkeypatch, tmp_path):
    from delivery.advertio_service import AdvertioDeliveryService

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"housing photo")
    record = {**_record(), "media_paths": [str(image)]}
    posted = []
    uploads = []

    def upload(path, source):
        uploads.append(source)
        return source + "-key"

    def post(payload):
        posted.append(payload)
        if len(posted) == 1:
            raise AdvertioError("transport timeout", retryable=True)
        return {"lead_id": "lead-1"}

    client = SimpleNamespace(upload_media=upload, create_lead=post)
    monkeypatch.setattr(config, "ADVERTIO_SOURCE_NAME", "housing-original")
    with pytest.raises(AdvertioError, match="transport timeout"):
        AdvertioDeliveryService(client=client).deliver(record, _housing())
    monkeypatch.setattr(config, "ADVERTIO_SOURCE_NAME", "housing-new")
    AdvertioDeliveryService(client=client).deliver(record, _housing())

    assert uploads == ["housing-original"]
    assert [payload["sourceName"] for payload in posted] == ["housing-original"] * 2
    assert posted[1]["mediaKeys"] == ["housing-original-key"]
