import os

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")

import pytest

import backoffice_data
import config
from storage import database
from storage import data_normalizer
from storage.message_repository import MessageRepository


@pytest.fixture
def norm_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "normalization.sqlite3"))
    database.initialize_db()
    data_normalizer.initialize()
    return tmp_path


def _insert_message(message_id=1, raw_text="raw source stays unchanged"):
    assert database.insert_message(
        "source", message_id, raw_text, "2026-09-29T00:00:00+00:00",
        raw_text=raw_text, cleaned_text=raw_text,
    )
    conn = database.get_connection()
    try:
        return conn.execute(
            "SELECT id FROM messages WHERE channel_username='source' AND message_id=?",
            (message_id,),
        ).fetchone()["id"]
    finally:
        conn.close()


def test_builtin_transfer_city_aliases_are_canonicalized(norm_db):
    data = {
        "origin_city": "Dusseldorf",
        "origin_country": "DE",
        "destination_city": "Frankfurt (Main)",
        "destination_country": "DE",
    }
    normalized, changes = data_normalizer.normalize_category_data("transferlist", data)

    assert normalized["origin_city"] == "Düsseldorf"
    assert normalized["destination_city"] == "Frankfurt"
    assert set(changes) == {"origin_city", "destination_city"}


@pytest.mark.parametrize("value,expected", [
    ("LA", "Los Angeles"),
    ("Imam Airport", "Tehran"),
    ("Imam Khomeini Airport", "Tehran"),
    ("Imam Khomeini International Airport", "Tehran"),
    ("Dusseldorf/Wuppertal", None),
])
def test_known_problem_values_have_safe_canonical_result(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "transferlist", {"origin_city": value, "origin_country": "DE"}
    )
    assert normalized["origin_city"] == expected


def test_alias_matching_is_case_and_accent_insensitive(norm_db):
    normalized, _ = data_normalizer.normalize_category_data(
        "transferlist", {"origin_city": "DÜSSELDORF"}
    )
    assert normalized["origin_city"] == "Düsseldorf"


def test_user_alias_can_target_any_structured_text_field(norm_db):
    data_normalizer.save_alias(
        "housinglist", "city", "NYC", "New York",
        notes="Back Office managed example", admin_id=1485409432,
    )
    normalized, changes = data_normalizer.normalize_category_data(
        "housinglist", {"city": "nyc", "country_code": "US"}
    )
    assert normalized["city"] == "New York"
    assert changes["city"] == ("nyc", "New York")


def test_country_scoped_alias_only_matches_related_country(norm_db):
    data_normalizer.save_alias(
        "transferlist", "origin_city", "Springfield", "Springfield, Illinois",
        country_iso2="US", admin_id=1485409432,
    )
    usa, _ = data_normalizer.normalize_category_data(
        "transferlist", {"origin_city": "Springfield", "origin_country": "US"}
    )
    canada, _ = data_normalizer.normalize_category_data(
        "transferlist", {"origin_city": "Springfield", "origin_country": "CA"}
    )
    assert usa["origin_city"] == "Springfield, Illinois"
    assert canada["origin_city"] == "Springfield"


def test_repository_saves_canonical_data_and_transfer_cache(norm_db):
    row_id = _insert_message()
    repo = MessageRepository()
    repo.initialize()
    repo.save_category_record(row_id, "transferlist", {
        "title": "Test",
        "origin_city": "Imam Airport",
        "origin_country": "IR",
        "destination_city": "Frankfurt (Main)",
        "destination_country": "DE",
    })

    conn = database.get_connection()
    try:
        transfer = conn.execute(
            "SELECT origin_city,destination_city FROM transferlist WHERE processed_message_id=?",
            (row_id,),
        ).fetchone()
        cache = conn.execute(
            "SELECT origin_city_canonical,destination_city_canonical FROM transfer_locations WHERE processed_message_id=?",
            (row_id,),
        ).fetchone()
        raw = conn.execute("SELECT raw_text FROM messages WHERE id=?", (row_id,)).fetchone()["raw_text"]
    finally:
        conn.close()

    assert transfer["origin_city"] == "Tehran"
    assert transfer["destination_city"] == "Frankfurt"
    assert cache["origin_city_canonical"] == "Tehran"
    assert cache["destination_city_canonical"] == "Frankfurt"
    assert raw == "raw source stays unchanged"


def test_backfill_cleans_existing_structured_rows_but_not_raw_message(norm_db):
    row_id = _insert_message()
    conn = database.get_connection()
    try:
        conn.execute("""INSERT INTO transferlist(
            processed_message_id,origin_city,origin_country,destination_city,destination_country
        ) VALUES(?,?,?,?,?)""", (row_id, "Dusseldorf", "DE", "LA", "US"))
        conn.commit()
    finally:
        conn.close()

    result = data_normalizer.renormalize_existing("transferlist")
    assert result["rows_changed"] == 1
    assert result["cells_changed"] == 2

    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT origin_city,destination_city FROM transferlist WHERE processed_message_id=?",
            (row_id,),
        ).fetchone()
        raw = conn.execute("SELECT raw_text FROM messages WHERE id=?", (row_id,)).fetchone()["raw_text"]
    finally:
        conn.close()
    assert row["origin_city"] == "Düsseldorf"
    assert row["destination_city"] == "Los Angeles"
    assert raw == "raw source stays unchanged"


def test_database_tab_manual_edit_is_normalized(norm_db):
    row_id = _insert_message()
    conn = database.get_connection()
    try:
        conn.execute("INSERT INTO transferlist(processed_message_id,origin_city) VALUES(?,?)",
                     (row_id, "Tehran"))
        transfer_id = conn.execute(
            "SELECT id FROM transferlist WHERE processed_message_id=?", (row_id,)
        ).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    value = backoffice_data.update_cell(
        "transferlist", transfer_id, "origin_city",
        "Frankfurt (Main)", "Tehran", 1485409432,
    )
    assert value == "Frankfurt"
    assert backoffice_data.cell("transferlist", transfer_id, "origin_city")["value"] == "Frankfurt"


@pytest.mark.parametrize("value,expected", [
    ("condo, house, townhouse, basement", "apartment"),
    ("apartment, condo, house, townhouse,   basement", "apartment"),
    ('["condo","house","townhouse","basement"]', "apartment"),
    ("multi", "apartment"),
    ("townhouse", "apartment"),
    ("null", None),
    ("none", None),
    ("n/a", None),
    ("", None),
])
def test_housing_property_type_defaults_are_normalized(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"property_type": value}
    )
    assert normalized["property_type"] == expected


@pytest.mark.parametrize("value,expected", [
    ("for sale", "sale"),
    ("FOR SALE", "sale"),
    ("sale", "sale"),
    ("null", None),
    ("", None),
])
def test_housing_listing_type_sale_defaults_are_normalized(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"listing_type": value}
    )
    assert normalized["listing_type"] == expected


@pytest.mark.parametrize("value,expected", [
    ("ON", "Ontario"),
    ("Ontario", "Ontario"),
    ("ontario", "Ontario"),
    ("QC", "Quebec"),
    ("Quebec", "Quebec"),
    ("Québec", "Quebec"),
    ("Qu√©bec", "Quebec"),
    ("BC", "British Columbia"),
    ("British Columbia", "British Columbia"),
    ("BRITISH COLUMBIA", "British Columbia"),
    ("NB", "New Brunswick"),
    ("New Brunswick", "New Brunswick"),
    ("SK", "Saskatchewan"),
    ("Northwest Territories", "Northwest Territories"),
    ("NT", "Northwest Territories"),
    ("AB", "Alberta"),
    ("MB", "Manitoba"),
    ("NL", "Newfoundland and Labrador"),
    ("NS", "Nova Scotia"),
    ("NU", "Nunavut"),
    ("PE", "Prince Edward Island"),
    ("YT", "Yukon"),
    ("null", None),
    ("", None),
])
def test_housing_canadian_province_defaults_use_full_names(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"province": value, "country_code": "CA"}
    )
    assert normalized["province"] == expected


def test_non_canadian_province_is_only_rejected_in_canada_scope(norm_db):
    canada, _ = data_normalizer.normalize_category_data(
        "housinglist", {"province": "Lazio", "country_code": "CA"}
    )
    italy, _ = data_normalizer.normalize_category_data(
        "housinglist", {"province": "Lazio", "country_code": "IT"}
    )
    assert canada["province"] is None
    assert italy["province"] == "Lazio"
