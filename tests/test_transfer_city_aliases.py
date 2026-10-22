"""Regression tests for 2026-10-22 CSV-reviewed transfer city aliases."""
import pytest

import config
from storage import database, data_normalizer
from storage.transfer_city_aliases import TRANSFER_CITY_ALIASES, TRANSFER_COUNTRY_ALIASES
from storage.message_repository import MessageRepository


@pytest.fixture
def city_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "cargo_city_aliases.sqlite"))
    database.initialize_db()
    data_normalizer.initialize()


@pytest.mark.parametrize("country,source,canonical", [
    ("IR", "Teheran", "Tehran"),
    ("IR", "تهران", "Tehran"),
    ("CN", "Guanzhou", "Guangzhou"),
    ("CN", "گوانژوو", "Guangzhou"),
    ("CN", "Peking", "Beijing"),
    ("CN", "Pekin", "Beijing"),
    ("CN", "Ханчжоу", "Hangzhou"),
    ("DE", "Hanover", "Hannover"),
    ("DE", "هامبورگ", "Hamburg"),
    ("DE", "Köln", "Cologne"),
    ("DE", "کلن", "Cologne"),
    ("DE", "Nurnberg", "Nuremberg"),
    ("RU", "Kalininigrad", "Kaliningrad"),
    ("RU", "Москва", "Moscow"),
    ("RU", "SPB", "St. Petersburg"),
    ("CA", "تورنتو", "Toronto"),
    ("CA", "Montréal", "Montreal"),
    ("TR", "Stambul", "Istanbul"),
    ("GB", "لندن", "London"),
    ("US", "Washington D.C.", "Washington, D.C."),
])
def test_city_aliases_work_on_both_route_sides(city_db, country, source, canonical):
    record = {
        "origin_city": source, "origin_country": country,
        "destination_city": source, "destination_country": country,
    }
    result, _ = data_normalizer.normalize_category_data("transferlist", record)
    assert result["origin_city"] == canonical
    assert result["destination_city"] == canonical


def test_seeded_city_aliases_are_country_scoped_and_cover_both_fields(city_db):
    aliases = data_normalizer.list_aliases()
    for country, alias, canonical, _ in TRANSFER_CITY_ALIASES:
        for field in ("origin_city", "destination_city"):
            subset = [x for x in aliases if x["category"] == "transferlist"
                      and x["field_name"] == field
                      and x["country_iso2"] == country
                      and data_normalizer._alias_key(x["alias"]) == data_normalizer._alias_key(alias)]
            assert subset and all(row["canonical_value"] == canonical for row in subset)
    wrong_country, _ = data_normalizer.normalize_category_data(
        "transferlist", {"origin_city": "تورنتو", "origin_country": "IR"}
    )
    assert wrong_country["origin_city"] == "تورنتو"


@pytest.mark.parametrize("old,new", [
    ("Iran", "IR"), ("ایران", "IR"), ("آلمان", "DE"),
    ("Germany", "DE"), ("China", "CN"), ("چین", "CN"),
    ("Türkiye", "TR"), ("USA", "US"),
])
def test_country_values_normalized_before_persian_city_aliases(city_db, old, new):
    data, _ = data_normalizer.normalize_category_data(
        "transferlist", {
            "origin_city": "تهران" if new == "IR" else "Berlin",
            "origin_country": old,
            "destination_city": "تورنتو",
            "destination_country": "Canada",
        }
    )
    assert data["origin_country"] == new
    assert data["destination_country"] == "CA"
    assert data["destination_city"] == "Toronto"
    if new == "IR":
        assert data["origin_city"] == "Tehran"


def test_ambiguous_country_as_city_and_multi_city_values_not_guessed(city_db):
    samples = [
        ("IR", "Iran"), ("CN", "China"), ("DE", "Germany"),
        ("CA", "Canada"), ("DE", "Munich and Stuttgart"),
        ("IR", "Tehran/Mashhad"), ("IT", "Turin/Milan"),
        ("DE", "Düsseldorf, Bochum"), ("CA", "North Vancouver"),
        ("US", "Maryland"),
    ]
    for country, value in samples:
        out, _ = data_normalizer.normalize_category_data(
            "transferlist", {
                "origin_city": value, "origin_country": country,
                "destination_city": value, "destination_country": country,
            }
        )
        assert out["origin_city"] == value
        assert out["destination_city"] == value


def test_user_custom_alias_is_not_overwritten_when_new_seeds_reload(city_db):
    record = next(x for x in data_normalizer.list_aliases()
                  if x["category"] == "transferlist" and x["field_name"] == "origin_city"
                  and x["country_iso2"] == "CN" and x["alias"] == "Guanzhou")
    data_normalizer.save_alias("transferlist", "origin_city", "Guanzhou",
                               "Guangzhou (custom)", country_iso2="CN", alias_id=record["id"])
    data_normalizer.initialize()
    out, _ = data_normalizer.normalize_category_data(
        "transferlist", {"origin_country": "CN", "origin_city": "Guanzhou"}
    )
    assert out["origin_city"] == "Guangzhou (custom)"


def test_backfill_only_structured_data_and_transfer_locations(city_db):
    assert database.insert_message(
        "cargo", 100, "raw text preserved", "2026-10-22",
        raw_text="raw text preserved",
    )
    conn = database.get_connection()
    try:
        row_id = conn.execute("SELECT id FROM messages WHERE message_id=100").fetchone()[0]
        conn.execute("""INSERT INTO transferlist(
            processed_message_id,origin_city,origin_country,destination_city,destination_country)
            VALUES(?,?,?,?,?)""",
            (row_id, "تهران", "ایران", "Köln", "Germany"))
        conn.commit()
    finally:
        conn.close()

    result = data_normalizer.renormalize_existing("transferlist")
    assert result["rows_changed"] == 1
    assert result["cells_changed"] == 6  # 4 route aliases + 2 provinces

    conn = database.get_connection()
    try:
        row = conn.execute("SELECT * FROM transferlist WHERE processed_message_id=?",
                           (row_id,)).fetchone()
        raw = conn.execute("SELECT raw_text FROM messages WHERE id=?", (row_id,)).fetchone()[0]
        cache = conn.execute("SELECT * FROM transfer_locations WHERE processed_message_id=?",
                             (row_id,)).fetchone()
    finally:
        conn.close()
    assert (row["origin_city"], row["origin_country"]) == ("Tehran", "IR")
    assert (row["destination_city"], row["destination_country"]) == ("Cologne", "DE")
    assert raw == "raw text preserved"
    assert cache["origin_city_canonical"] == "Tehran"
    assert cache["destination_city_canonical"] == "Cologne"


def test_route_only_backfill_does_not_change_descriptions_or_status(city_db):
    database.insert_message(
        "cargo", 220, "travel ad", "2026-10-22",
        raw_text="do not change this Telegram message",
    )
    conn = database.get_connection()
    try:
        row_id = conn.execute("SELECT id FROM messages WHERE message_id=220").fetchone()[0]
        conn.execute("""INSERT INTO transferlist(
            processed_message_id,title,description,origin_city,origin_country,
            destination_city,destination_country,departure_date)
            VALUES(?,?,?,?,?,?,?,?)""",
            (row_id, "Original title", "Original cleaned description",
             "Guanzhou", "China", "تهران", "Iran", "2026-11-07"))
        conn.execute("UPDATE messages SET advertio_status='sent' WHERE id=?", (row_id,))
        conn.commit()
    finally:
        conn.close()

    result = data_normalizer.renormalize_transfer_cities_only()
    assert result == {"rows_scanned": 1, "rows_changed": 1, "cells_changed": 4}
    again = data_normalizer.renormalize_transfer_cities_only()
    assert again["rows_changed"] == again["cells_changed"] == 0

    conn = database.get_connection()
    try:
        row = conn.execute("SELECT * FROM transferlist WHERE processed_message_id=?",
                           (row_id,)).fetchone()
        msg = conn.execute("SELECT * FROM messages WHERE id=?", (row_id,)).fetchone()
        cache = conn.execute("SELECT * FROM transfer_locations WHERE processed_message_id=?",
                             (row_id,)).fetchone()
    finally:
        conn.close()

    assert (row["origin_city"], row["origin_country"]) == ("Guangzhou", "CN")
    assert (row["destination_city"], row["destination_country"]) == ("Tehran", "IR")
    assert row["description"] == "Original cleaned description"
    assert row["title"] == "Original title"
    assert row["departure_date"] == "2026-11-07"
    assert msg["raw_text"] == "do not change this Telegram message"
    assert msg["advertio_status"] == "sent"
    assert cache["origin_city_canonical"] == "Guangzhou"
