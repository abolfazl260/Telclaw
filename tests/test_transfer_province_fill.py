"""Province/state autofill using exact city+country, with opt-in historical backfill."""
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

import backoffice_web
import config
from storage import database, data_normalizer
from storage.transfer_province_map import (
    PROVINCE_BY_COUNTRY_AND_CITY, lookup_province,
)


@pytest.fixture
def province_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "province-normalization.sqlite"))
    database.initialize_db()
    data_normalizer.initialize()


@pytest.mark.parametrize("country,city,expected", [
    ("IR", "Tehran", "Tehran"),
    ("IR", "Teheran", "Tehran"),
    ("IR", "تهران", "Tehran"),
    ("IR", "Mashhad", "Razavi Khorasan"),
    ("IR", "Karaj", "Alborz"),
    ("IR", "Shiraz", "Fars"),
    ("CA", "Toronto", "Ontario"),
    ("CA", "تورنتو", "Ontario"),
    ("CA", "Vancouver", "British Columbia"),
    ("CA", "Montreal", "Quebec"),
    ("CA", "London", "Ontario"),
    ("DE", "Düsseldorf", "North Rhine-Westphalia"),
    ("DE", "Koln", "North Rhine-Westphalia"),
    ("DE", "Berlin", "Berlin"),
    ("DE", "Frankfurt", "Hesse"),
    ("DE", "Munich", "Bavaria"),
    ("CN", "Guangzhou", "Guangdong"),
    ("CN", "Guanzhou", "Guangdong"),
    ("CN", "Peking", "Beijing"),
    ("CN", "Shanghai", "Shanghai"),
    ("RU", "Moscow", "Moscow"),
    ("RU", "Kalininigrad", "Kaliningrad Oblast"),
    ("IT", "Rome", "Lazio"),
    ("IT", "Milan", "Lombardy"),
    ("TR", "Istanbul", "Istanbul"),
    ("AE", "Dubai", "Dubai"),
    ("US", "Los Angeles", "California"),
    ("US", "Dallas", "Texas"),
    ("AU", "Sydney", "New South Wales"),
    ("AT", "Vienna", "Vienna"),
    ("DK", "Copenhagen", "Capital Region of Denmark"),
])
def test_known_csv_city_country_yields_province(province_db, country, city, expected):
    assert lookup_province(country, city) == expected
    row = {"origin_city": city, "origin_country": country,
           "destination_city": city, "destination_country": country}
    normalized, changes = data_normalizer.normalize_category_data("transferlist", row)
    assert normalized["origin_province"] == expected
    assert normalized["destination_province"] == expected
    assert changes["origin_province"] == (None, expected)


def test_province_mapping_has_no_country_city_collisions():
    assert len(PROVINCE_BY_COUNTRY_AND_CITY) >= 140


@pytest.mark.parametrize("country,city", [
    ("IR", "Iran"), ("CN", "China"), ("DE", "Germany"),
    ("CA", "Canada"), ("DE", "Munich and Stuttgart"),
    ("IR", "Tehran/Mashhad"), ("CA", "Toronto/Vancouver"),
    ("DE", "Düsseldorf, Bochum"), ("US", "Orange County"),
    ("US", "Maryland"), ("CA", "Munich"),
    ("CN", "Tehran"), ("IR", "Beijing"), ("", "Tehran"),
    ("US/CA", "Toronto"), ("US", "Washington"),
])
def test_ambiguous_and_mismatched_routes_never_autofill(province_db, country, city):
    assert lookup_province(country, city) is None
    normalized, changes = data_normalizer.normalize_category_data(
        "transferlist", {"origin_city": city, "origin_country": country}
    )
    assert "origin_province" not in changes
    assert not normalized.get("origin_province")


def test_new_cargo_normalizes_country_city_before_province_and_preserves_existing(province_db):
    normalized, changes = data_normalizer.normalize_category_data(
        "transferlist", {
            "origin_country": "ایران", "origin_city": "تهران",
            "destination_country": "Canada", "destination_city": "تورنتو",
            "destination_province": "Custom operator province",
        }
    )
    assert normalized["origin_country"] == "IR"
    assert normalized["origin_city"] == "Tehran"
    assert normalized["origin_province"] == "Tehran"
    assert normalized["destination_country"] == "CA"
    assert normalized["destination_city"] == "Toronto"
    assert normalized["destination_province"] == "Custom operator province"
    assert "destination_province" not in changes


def _add_cargo(conn, message_id, *, country="IR", city="تهران",
               dest_country="CA", dest_city="تورنتو",
               origin_province=None, destination_province=None,
               description="Original cleaned cargo description"):
    conn.execute("""INSERT INTO messages(channel_username,message_id,date,raw_text,
                    advertio_status) VALUES(?,?,?,?,?)""",
                 ("cargo", message_id, "2026-10-22", "raw source do not modify", "sent"))
    row_id = conn.execute(
        "SELECT id FROM messages WHERE channel_username='cargo' AND message_id=?",
        (message_id,),
    ).fetchone()[0]
    conn.execute("""INSERT INTO transferlist(
        processed_message_id,origin_country,origin_city,origin_province,
        destination_country,destination_city,destination_province,description,
        departure_date) VALUES(?,?,?,?,?,?,?,?,?)""",
        (row_id, country, city, origin_province,
         dest_country, dest_city, destination_province, description, "2026-11-25"))
    return row_id


def test_historical_preview_apply_and_idempotency_are_field_scoped(province_db):
    conn = database.get_connection()
    try:
        a = _add_cargo(conn, 3001)
        b = _add_cargo(conn, 3002, origin_province="Old explicit location",
                       dest_country="CN", dest_city="Guanzhou")
        c = _add_cargo(conn, 3003, country="IR", city="Iran",
                       dest_country="DE", dest_city="Munich and Stuttgart")
        d = _add_cargo(conn, 3004, origin_province=" ",
                       destination_province="Ontario")
        conn.commit()
    finally:
        conn.close()

    preview = data_normalizer.preview_transfer_missing_provinces()
    assert preview["rows_scanned"] == 4
    assert preview["rows_to_fill"] == 3
    assert preview["fields_to_fill"] == 4
    assert preview["origin_to_fill"] == 2
    assert preview["destination_to_fill"] == 2
    assert preview["existing_mismatches"] == 1
    assert preview["rows_updated"] == 0

    conn = database.get_connection()
    try:
        assert conn.execute(
            "SELECT origin_province FROM transferlist WHERE processed_message_id=?",
            (a,),
        ).fetchone()[0] is None
    finally:
        conn.close()

    applied = data_normalizer.fill_transfer_missing_provinces()
    assert applied["rows_to_fill"] == 3
    assert applied["fields_to_fill"] == applied["rows_updated"] == 4
    again = data_normalizer.fill_transfer_missing_provinces()
    assert again["rows_to_fill"] == again["rows_updated"] == 0
    assert again["existing_mismatches"] == 1

    conn = database.get_connection()
    try:
        records = {row["processed_message_id"]: row for row in conn.execute("SELECT * FROM transferlist")}
        messages = list(conn.execute("SELECT raw_text,advertio_status FROM messages"))
    finally:
        conn.close()
    assert records[a]["origin_province"] == "Tehran"
    assert records[a]["destination_province"] == "Ontario"
    assert records[b]["origin_province"] == "Old explicit location"
    assert records[b]["destination_province"] == "Guangdong"
    assert records[c]["origin_province"] is None
    assert records[c]["destination_province"] is None
    assert records[d]["origin_province"] == "Tehran"
    assert records[d]["destination_province"] == "Ontario"
    assert all(row["raw_text"] == "raw source do not modify"
               and row["advertio_status"] == "sent" for row in messages)
    assert all(records[i]["description"] == "Original cleaned cargo description"
               and records[i]["departure_date"] == "2026-11-25" for i in records)


@pytest.mark.asyncio
async def test_backoffice_preview_is_opt_in_and_backfill_requires_csrf(province_db, monkeypatch):
    # create_app initializes the private session table before adding a session.
    client = TestClient(TestServer(backoffice_web.create_app()))
    conn = database.get_connection()
    try:
        row_id = _add_cargo(conn, 4001)
        conn.commit()
        conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                     (backoffice_web._digest("province-test-session"), 1485409432,
                      "csrf-for-province", (backoffice_web._now() +
                       timedelta(hours=1)).isoformat()))
        conn.commit()
    finally:
        conn.close()

    real_preview = data_normalizer.preview_cargo_location_normalization
    calls = []
    def track_preview():
        calls.append(True)
        return real_preview()
    monkeypatch.setattr(data_normalizer, "preview_cargo_location_normalization", track_preview)

    await client.start_server()
    try:
        headers = {"Cookie": "telclaw_admin=province-test-session"}
        response = await client.get("/normalization", headers=headers)
        assert response.status == 200
        html = await response.text()
        assert "Cargo Location Normalization" in html
        assert "Preview Changes" in html
        assert "Apply Normalization" in html
        assert "<details id=\"advanced-normalization\">" in html
        assert calls == []

        response = await client.get("/normalization?cargo_preview=1", headers=headers)
        assert response.status == 200
        body = await response.text()
        assert "missing provinces: 2" in body
        assert calls == [True]

        bad = await client.post("/normalization/apply-cargo-locations",
                                data={"csrf": "incorrect"}, headers=headers)
        assert bad.status == 403
        response = await client.post(
            "/normalization/apply-cargo-locations",
            data={"csrf": "csrf-for-province"}, headers=headers, allow_redirects=False,
        )
        assert response.status == 303
        assert "cargo_applied" in response.headers["Location"]
        conn = database.get_connection()
        try:
            row = conn.execute("SELECT origin_province,destination_province FROM transferlist WHERE processed_message_id=?",
                               (row_id,)).fetchone()
        finally:
            conn.close()
        assert tuple(row) == ("Tehran", "Ontario")
    finally:
        await client.close()


def test_unified_cargo_preview_atomic_apply_and_idempotency(province_db):
    conn = database.get_connection()
    try:
        first = _add_cargo(
            conn, 8001, country="ایران", city="Teheran",
            dest_country="Canada", dest_city="تورنتو",
        )
        second = _add_cargo(
            conn, 8002, country="RU", city="Moscow/Naberezhnye Chelny",
            dest_country="AE", dest_city="Air Tour",
        )
        third = _add_cargo(
            conn, 8003, country="IR", city="Tehran/Karaj",
            origin_province="Alborz", dest_country="CN", dest_city="China",
        )
        fourth = _add_cargo(
            conn, 8004, country="CA", city="Toronto",
            origin_province="Ontario", dest_country="DE", dest_city="Munich",
            destination_province="Hesse",
        )
        fifth = _add_cargo(
            conn, 8005, country="IR", city="Tehran/Mashhad",
            dest_country="CN", dest_city="China",
        )
        conn.commit()
    finally:
        conn.close()

    preview = data_normalizer.preview_cargo_location_normalization()
    assert preview == {
        "rows_scanned": 5,
        "rows_to_change": 3,
        "fields_to_change": 10,
        "country_changes": 2,
        "city_changes": 5,
        "provinces_to_fill": 3,
        "existing_province_mismatches": 2,
    }
    conn = database.get_connection()
    try:
        before = conn.execute(
            "SELECT origin_country,origin_city,origin_province "
            "FROM transferlist WHERE processed_message_id=?", (first,)
        ).fetchone()
    finally:
        conn.close()
    assert tuple(before) == ("ایران", "Teheran", None)

    applied = data_normalizer.apply_cargo_location_normalization()
    assert applied == preview
    again = data_normalizer.apply_cargo_location_normalization()
    assert again["rows_to_change"] == again["fields_to_change"] == 0
    assert again["existing_province_mismatches"] == 2

    conn = database.get_connection()
    try:
        rows = {
            row["processed_message_id"]: row for row in
            conn.execute("SELECT * FROM transferlist")
        }
        messages = list(conn.execute("SELECT raw_text,advertio_status FROM messages"))
        cache = conn.execute(
            "SELECT origin_city_canonical,origin_country_iso2,destination_city_canonical "
            "FROM transfer_locations WHERE processed_message_id=?", (first,),
        ).fetchone()
    finally:
        conn.close()
    assert (rows[first]["origin_country"], rows[first]["origin_city"],
            rows[first]["origin_province"]) == ("IR", "Tehran", "Tehran")
    assert (rows[first]["destination_country"], rows[first]["destination_city"],
            rows[first]["destination_province"]) == ("CA", "Toronto", "Ontario")
    assert (rows[second]["origin_city"], rows[second]["origin_province"]) == ("Moscow", "Moscow")
    assert rows[second]["destination_city"] is None
    assert rows[second]["destination_province"] is None
    assert rows[third]["origin_city"] == "Tehran"
    assert rows[third]["origin_province"] == "Alborz"  # never replace an explicit value
    assert rows[fourth]["destination_province"] == "Hesse"
    assert rows[fifth]["origin_city"] == "Tehran/Mashhad"
    assert all(row["description"] == "Original cleaned cargo description"
               and row["departure_date"] == "2026-11-25" for row in rows.values())
    assert all(row["raw_text"] == "raw source do not modify"
               and row["advertio_status"] == "sent" for row in messages)
    assert tuple(cache) == ("Tehran", "IR", "Toronto")


def test_unified_cargo_apply_rolls_back_every_field_on_cache_sync_failure(province_db, monkeypatch):
    conn = database.get_connection()
    try:
        row_id = _add_cargo(
            conn, 8101, country="ایران", city="Teheran",
            dest_country="Canada", dest_city="تورنتو",
        )
        conn.commit()
    finally:
        conn.close()

    def fail_cache_sync(*args):
        raise RuntimeError("simulated transfer cache failure")

    monkeypatch.setattr(data_normalizer, "_sync_transfer_location", fail_cache_sync)
    with pytest.raises(RuntimeError, match="simulated"):
        data_normalizer.apply_cargo_location_normalization()

    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT origin_country,origin_city,origin_province,destination_country,"
            "destination_city,destination_province FROM transferlist "
            "WHERE processed_message_id=?", (row_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("ایران", "Teheran", None, "Canada", "تورنتو", None)


@pytest.mark.asyncio
async def test_unified_location_preview_only_on_demand_with_same_admin_auth(province_db, monkeypatch):
    client = TestClient(TestServer(backoffice_web.create_app()))
    conn = database.get_connection()
    try:
        _add_cargo(conn, 8201, country="ایران", city="Teheran",
                   dest_country="Canada", dest_city="تورنتو")
        conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                     (backoffice_web._digest("unified-test-session"), 1485409432,
                      "unified-csrf", (backoffice_web._now() +
                       timedelta(hours=1)).isoformat()))
        conn.commit()
    finally:
        conn.close()

    actual_preview = data_normalizer.preview_cargo_location_normalization
    calls = []
    def measured_preview():
        calls.append("preview")
        return actual_preview()
    monkeypatch.setattr(data_normalizer, "preview_cargo_location_normalization",
                        measured_preview)
    await client.start_server()
    try:
        headers = {"Cookie": "telclaw_admin=unified-test-session"}
        unauthed = await client.get("/normalization?cargo_preview=1")
        assert unauthed.status == 401

        page = await client.get("/normalization", headers=headers)
        assert page.status == 200
        html = await page.text()
        assert "Cargo Location Normalization" in html
        assert 'action="/normalization/apply-cargo-locations"' in html
        assert '<details id="advanced-normalization">' in html
        assert "Preview Changes" in html
        assert "Apply Normalization" in html
        assert 'action="/normalization/apply-transfer-cities"' not in html
        assert 'action="/normalization/fill-transfer-provinces"' not in html
        assert calls == []

        preview = await client.get("/normalization?cargo_preview=1", headers=headers)
        assert preview.status == 200
        rendered = await preview.text()
        assert "Read-only preview" in rendered
        assert "Countries: 2" in rendered
        assert "cities: 2" in rendered
        assert "missing provinces: 2" in rendered
        assert calls == ["preview"]

        invalid = await client.post(
            "/normalization/apply-cargo-locations",
            data={"csrf": "incorrect"}, headers=headers,
        )
        assert invalid.status == 403
        applied = await client.post(
            "/normalization/apply-cargo-locations",
            data={"csrf": "unified-csrf"}, headers=headers,
            allow_redirects=False,
        )
        assert applied.status == 303
        assert "cargo_applied" in applied.headers["Location"]
    finally:
        await client.close()
