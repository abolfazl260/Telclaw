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

    real_preview = data_normalizer.preview_transfer_missing_provinces
    calls = []
    def track_preview():
        calls.append(True)
        return real_preview()
    monkeypatch.setattr(data_normalizer, "preview_transfer_missing_provinces", track_preview)

    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        headers = {"Cookie": "telclaw_admin=province-test-session"}
        response = await client.get("/normalization", headers=headers)
        assert response.status == 200
        html = await response.text()
        assert "Fill missing cargo provinces" in html
        assert "Preview matching empty provinces" in html
        assert "Apply cargo city and country rules" in html
        assert calls == []

        response = await client.get("/normalization?province_preview=1", headers=headers)
        assert response.status == 200
        body = await response.text()
        assert "2 missing province field(s)" in body
        assert calls == [True]

        bad = await client.post("/normalization/fill-transfer-provinces",
                                data={"csrf": "incorrect"}, headers=headers)
        assert bad.status == 403
        response = await client.post(
            "/normalization/fill-transfer-provinces",
            data={"csrf": "csrf-for-province"}, headers=headers, allow_redirects=False,
        )
        assert response.status == 303
        assert "provinces_filled" in response.headers["Location"]
        conn = database.get_connection()
        try:
            row = conn.execute("SELECT origin_province,destination_province FROM transferlist WHERE processed_message_id=?",
                               (row_id,)).fetchone()
        finally:
            conn.close()
        assert tuple(row) == ("Tehran", "Ontario")
    finally:
        await client.close()
