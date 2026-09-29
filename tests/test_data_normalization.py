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


@pytest.mark.parametrize("value,expected", [
    ("Bernaby", "Burnaby"),
    ("C√¥te-Saint-Luc", "Côte-Saint-Luc"),
    ("Montr√©al", "Montreal"),
    ("Richmondhill", "Richmond Hill"),
    ("Multiple", None),
    ("Multiple cities", None),
    ("RICHMOND HILL & AURORA", None),
    ("Rome or Blonay", None),
    ("Ram", None),
    ("Rim", None),
    ("Rum", None),
    ("Rumiyah", None),
    ("Lanzadel", None),
    ("Terrenee", None),
    ("Tehran (Rum)", None),
    ("Caserta", None),
    ("Cookeville", None),
    ("D√ºsseldorf", None),
    ("Frankfurt", None),
    ("K√∂ln", None),
    ("Mashhad", None),
    ("Roma", None),
    ("Tehran", None),
    ("Torino", None),
    ("Turin", None),
    ("Lazio", None),
])
def test_housing_city_safe_canadian_defaults(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"city": value, "country_code": "CA", "province": "Ontario"}
    )
    assert normalized["city"] == expected


@pytest.mark.parametrize("value", [
    "Rum (likely referring to Tehran, Iran, but assumed to be in Canada for this example)",
    "Rum (Rum, Iran) -> ambiguous mapping, skipping",
    "Rum (Tehran Province)",
])
def test_housing_city_rejects_rum_explanation_artifacts(norm_db, value):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"city": value, "country_code": "CA", "province": "Ontario"}
    )
    assert normalized["city"] is None


@pytest.mark.parametrize("province,value,expected", [
    ("Ontario", "Don Mills", "Toronto"),
    ("Ontario", "Don Valley", "Toronto"),
    ("Ontario", "East Woodbridge", "Vaughan"),
    ("Ontario", "Maple", "Vaughan"),
    ("Ontario", "North York", "Toronto"),
    ("Ontario", "Oak Ridges", "Richmond Hill"),
    ("Ontario", "Yonge / Finch", "Toronto"),
    ("British Columbia", "North Burnaby", "Burnaby"),
    ("British Columbia", "Yaletown", "Vancouver"),
])
def test_housing_city_neighborhoods_require_matching_province(norm_db, province, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"city": value, "country_code": "CA", "province": province}
    )
    assert normalized["city"] == expected


def test_housing_city_neighborhood_is_not_rewritten_for_wrong_province(norm_db):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"city": "North York", "country_code": "CA", "province": "British Columbia"}
    )
    assert normalized["city"] == "North York"


@pytest.mark.parametrize("value", ["Bathurst", "Bayview", "Rutherford", "Thornhill", "Langdale"])
def test_housing_city_ambiguous_values_are_left_untouched(norm_db, value):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"city": value, "country_code": "CA", "province": "Ontario"}
    )
    assert normalized["city"] == value


def test_housing_city_non_canadian_cleanup_is_scoped_to_canada(norm_db):
    canada, _ = data_normalizer.normalize_category_data(
        "housinglist", {"city": "Tehran", "country_code": "CA", "province": "Ontario"}
    )
    iran, _ = data_normalizer.normalize_category_data(
        "housinglist", {"city": "Tehran", "country_code": "IR", "province": "Tehran"}
    )
    assert canada["city"] is None
    assert iran["city"] == "Tehran"


@pytest.mark.parametrize("value,expected", [
    ("Bathurs", "Bathurst"),
    ("Cookeilam", "Coquitlam"),
    ("Bayview and Sheppard", "Bayview & Sheppard"),
    ("Bayview and Major McKenzie Dr", "Bayview & Major Mackenzie"),
    ("Marine Dr", "Marine Drive"),
    ("Marin drive", "Marine Drive"),
    ("Finch/Yong", "Yonge & Finch"),
    ("Finch & Yonge", "Yonge & Finch"),
    ("Yonge / Finch", "Yonge & Finch"),
    ("Yonge and Finch", "Yonge & Finch"),
    ("Yonge&Finch", "Yonge & Finch"),
    ("Young and Finch", "Yonge & Finch"),
    ("Yang and Finch", "Yonge & Finch"),
    ("Finnch and Bathurst", "Finch & Bathurst"),
    ("Finch and Bathurst", "Finch & Bathurst"),
    ("Finch and Bath", "Finch & Bathurst"),
    ("Yonge/Sheppard", "Yonge & Sheppard"),
    ("Yonge and Sheppard", "Yonge & Sheppard"),
    ("Near Yonge and Sheppard", "Yonge & Sheppard"),
    ("Yonge / Steeles", "Yonge & Steeles"),
    ("Yonge and Steeles", "Yonge & Steeles"),
    ("Yonge steeles", "Yonge & Steeles"),
    ("Yonge & Steels", "Yonge & Steeles"),
    ("Yonge & Elginmills", "Yonge & Elgin Mills"),
    ("Yonge and Elgin Mills", "Yonge & Elgin Mills"),
    ("Steeles &Bayview", "Bayview & Steeles"),
    ("Sheppard & Donmills", "Don Mills & Sheppard"),
    ("Shepherd and 404", "Sheppard & Hwy 404"),
    ("Victoria Park and Shepherd", "Victoria Park & Sheppard"),
    ("Weston Rd and black creek", "Weston Road & Black Creek"),
    ("Weston Road and Black Creek", "Weston Road & Black Creek"),
    ("Upper Lansdale", "Upper Lonsdale"),
])
def test_housing_neighborhood_safe_aliases(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist",
        {"neighborhood": value, "country_code": "CA", "province": "Ontario", "city": "Toronto"},
    )
    assert normalized["neighborhood"] == expected


@pytest.mark.parametrize("value", [
    "Multiple",
    "Multiple neighborhoods",
    "Various",
    "A metro area",
    "C/A/B metro area",
    "center",
    "central area",
    "Metropolitan A",
    "Lower",
    "LOWER - 328 MOORE PARK AVENUE",
    "Regions 14, 3, 2, 15",
    "Rum",
    '["Dun Tan","North   York","Thornhill","Richmond   Hill","Aurora","Newmarket"]',
    "North Vancouver, Coquitlam, West Vancouver, Port Coquitlam, Downtown, Burnaby",
    "North York, Downtown Toronto, Midtown Toronto, Richmond Hill, Vaughan, Markham, Aurora, Newmarket, Scarborough, Etobicoke",
    "North York, Richmond Hill, Markham, Vaughan, Newmarket",
    "North York, Richmond Hill, Markham, Vaughan, Newmarket, GTA",
    "Vancouver, Burnaby, New Westminster",
    "ÿ¢ÿ±Ÿàÿ±ÿß",
    "ÿßÿ±Ÿàÿ±ÿß",
    "ŸÜŸàÿ±ÿ™ €åŸàÿ±⁄©",
])
def test_housing_neighborhood_invalid_or_multi_values_become_null(norm_db, value):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist",
        {"neighborhood": value, "country_code": "CA", "province": "Ontario", "city": "Toronto"},
    )
    assert normalized["neighborhood"] is None


@pytest.mark.parametrize("province,city,value,expected", [
    ("British Columbia", "North Vancouver", "North Vancouver - Delbrook", "Delbrook"),
    ("British Columbia", "North Vancouver", "North Vancouver - Upper Lonsdale", "Upper Lonsdale"),
    ("British Columbia", "West Vancouver", "West Vancouver Ambleside", "Ambleside"),
    ("British Columbia", "Langley", "Brookswood, Langley", "Brookswood"),
    ("Ontario", "Richmond Hill", "Richmond Hill - West Brook", "Westbrook"),
    ("Ontario", "Richmond Hill", "Oak Ridges Richmond Hill", "Oak Ridges"),
    ("Ontario", "Toronto", "Midtown (Yonge and Eglinton)", "Midtown"),
])
def test_housing_neighborhood_contextual_aliases(norm_db, province, city, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist",
        {"neighborhood": value, "country_code": "CA", "province": province, "city": city},
    )
    assert normalized["neighborhood"] == expected


def test_housing_neighborhood_contextual_alias_requires_matching_city(norm_db):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist",
        {
            "neighborhood": "North Vancouver - Delbrook",
            "country_code": "CA",
            "province": "British Columbia",
            "city": "Vancouver",
        },
    )
    assert normalized["neighborhood"] == "North Vancouver - Delbrook"


@pytest.mark.parametrize("value", [
    "Bayview",
    "Bathurst",
    "Thornhill",
    "Downtown",
    "Brentwood",
    "Victoria Park",
    "Yonge",
    "Lonsdale",
    "Finch and Bloor",
    "Finnch and Bloor",
])
def test_housing_neighborhood_ambiguous_values_are_preserved(norm_db, value):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist",
        {"neighborhood": value, "country_code": "CA", "province": "Ontario", "city": "Toronto"},
    )
    assert normalized["neighborhood"] == value


def test_housing_neighborhood_rules_are_scoped_to_canada(norm_db):
    canada, _ = data_normalizer.normalize_category_data(
        "housinglist",
        {"neighborhood": "Multiple", "country_code": "CA", "province": "Ontario", "city": "Toronto"},
    )
    italy, _ = data_normalizer.normalize_category_data(
        "housinglist",
        {"neighborhood": "Multiple", "country_code": "IT", "province": "Lazio", "city": "Rome"},
    )
    assert canada["neighborhood"] is None
    assert italy["neighborhood"] == "Multiple"


@pytest.mark.parametrize("value,expected", [
    ("1 ŸÖÿßŸáŸá", "short_term"),
    ("1-6 months", "short_term"),
    ("2 months", "short_term"),
    ("3 months", "short_term"),
    ("6 months", "short_term"),
    ("6 months or less", "short_term"),
    ("daily", "daily"),
    ("month", "long_term"),
    ("monthly", "long_term"),
    ("Monthly / Short-Term", "short_term"),
    ("Monthly, Short-Term", "short_term"),
    ("one month", "short_term"),
    ("short-term", "short_term"),
    ("weekly", "short_term"),
    ("weekly and monthly", "short_term"),
    ("year", "long_term"),
    ("yearly", "long_term"),
    ("null", None),
    ("", None),
])
def test_housing_rent_period_defaults_match_advertio_duration_enum(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"rent_period": value}
    )
    assert normalized["rent_period"] == expected


@pytest.mark.parametrize("value,expected", [
    ("0", "0"),
    ("1", "1"),
    ("2", "2"),
    ("3", "3"),
    ("4", "4+"),
    ("4+", "4+"),
    ("5", "4+"),
    ("6", "4+"),
    ("one", "1"),
    ("Bachelor", "0"),
    ("1+Den", "1"),
    ("1+1", "1"),
    ("2+1", "2"),
    ("4+1", "4+"),
    ("master", "1"),
    ("single", "0"),
    ("1 person or 1 couple", None),
    ("single or double", "0"),
    ("single/double", "0"),
    ("single and double", None),
    ("0, 1, 2, 3, 4+", "1"),
    ("1, 2, 3+", "1"),
    ("1, 2, 2+", "2"),
    ("1, 2, 3, 4+", "1"),
    ("1, 2, 3", "1"),
    ("2-Jan", "0"),
    ("2 or 1", "1"),
    ("2 or 3", "2"),
    ("1+", None),
    ("2+", None),
    ("3+", None),
    ("3-Jan", None),
    ("5¬Ω", None),
    ("null", None),
    ("", None),
])
def test_housing_bedrooms_defaults_match_advertio_enum(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"bedrooms": value}
    )
    assert normalized["bedrooms"] == expected


def test_housing_bedrooms_schema_migrates_integer_to_text_without_data_loss(tmp_path, monkeypatch):
    db_path = tmp_path / "bedrooms-migration.sqlite3"
    monkeypatch.setattr(config, "DB_NAME", str(db_path))

    conn = database.get_connection()
    try:
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            text TEXT,
            date TEXT NOT NULL,
            UNIQUE(channel_username,message_id)
        )""")
        conn.execute("""CREATE TABLE housinglist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            processed_message_id INTEGER NOT NULL UNIQUE,
            bedrooms INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE
        )""")
        conn.execute("INSERT INTO messages(channel_username,message_id,text,date) VALUES('source',1,'x','2026-09-29')")
        message_id = conn.execute("SELECT id FROM messages WHERE message_id=1").fetchone()["id"]
        conn.execute("INSERT INTO housinglist(processed_message_id,bedrooms) VALUES(?,?)", (message_id, 2))
        conn.commit()
    finally:
        conn.close()

    database.initialize_db()

    conn = database.get_connection()
    try:
        schema = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(housinglist)")}
        row = conn.execute("SELECT bedrooms FROM housinglist").fetchone()
    finally:
        conn.close()

    assert schema["bedrooms"] == "TEXT"
    assert row["bedrooms"] == "2"


def test_bedrooms_is_available_in_normalization_backoffice_targets(norm_db):
    assert "bedrooms" in data_normalizer.aliasable_fields()["housinglist"]


def test_database_tab_bedrooms_edit_uses_normalization_rules(norm_db):
    row_id = _insert_message(message_id=77)
    conn = database.get_connection()
    try:
        conn.execute("INSERT INTO housinglist(processed_message_id,bedrooms) VALUES(?,?)", (row_id, "2"))
        housing_id = conn.execute(
            "SELECT id FROM housinglist WHERE processed_message_id=?", (row_id,)
        ).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    value = backoffice_data.update_cell(
        "housinglist", housing_id, "bedrooms", "master", "2", 1485409432,
    )
    assert value == "1"
    assert backoffice_data.cell("housinglist", housing_id, "bedrooms")["value"] == "1"

    value = backoffice_data.update_cell(
        "housinglist", housing_id, "bedrooms", "4", "1", 1485409432,
    )
    assert value == "4+"
    assert backoffice_data.cell("housinglist", housing_id, "bedrooms")["value"] == "4+"


@pytest.mark.parametrize("value,expected", [
    (2, 2),
    (1, 1),
    (3.5, 3),
    (0, None),
    (3, 3),
    (2.5, 2),
    ("null", None),
    (4, 4),
    (5, 5),
    (1.5, 1),
    ("2+1", 2),
    ("", None),
])
def test_housing_bathrooms_are_rounded_down(norm_db, value, expected):
    normalized, _ = data_normalizer.normalize_category_data(
        "housinglist", {"bathrooms": value}
    )
    assert normalized["bathrooms"] == expected


def test_housing_bathrooms_schema_migrates_real_to_integer_and_floors_existing_values(tmp_path, monkeypatch):
    db_path = tmp_path / "bathrooms-migration.sqlite3"
    monkeypatch.setattr(config, "DB_NAME", str(db_path))

    conn = database.get_connection()
    try:
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            text TEXT,
            date TEXT NOT NULL,
            UNIQUE(channel_username,message_id)
        )""")
        conn.execute("""CREATE TABLE housinglist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            processed_message_id INTEGER NOT NULL UNIQUE,
            bathrooms REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE
        )""")
        for message_id, bathrooms in [(1, 3.5), (2, 2.5), (3, 0), (4, "2+1")]:
            conn.execute(
                "INSERT INTO messages(channel_username,message_id,text,date) VALUES('source',?,'x','2026-09-29')",
                (message_id,),
            )
            processed_id = conn.execute(
                "SELECT id FROM messages WHERE message_id=?", (message_id,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO housinglist(processed_message_id,bathrooms) VALUES(?,?)",
                (processed_id, bathrooms),
            )
        conn.commit()
    finally:
        conn.close()

    database.initialize_db()

    conn = database.get_connection()
    try:
        schema = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(housinglist)")}
        values = [row["bathrooms"] for row in conn.execute("SELECT bathrooms FROM housinglist ORDER BY id")]
    finally:
        conn.close()

    assert schema["bathrooms"] == "INTEGER"
    assert values == [3, 2, None, 2]


def test_database_tab_bathrooms_edit_rounds_down(norm_db):
    row_id = _insert_message(message_id=88)
    conn = database.get_connection()
    try:
        conn.execute("INSERT INTO housinglist(processed_message_id,bathrooms) VALUES(?,?)", (row_id, 2))
        housing_id = conn.execute(
            "SELECT id FROM housinglist WHERE processed_message_id=?", (row_id,)
        ).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    value = backoffice_data.update_cell(
        "housinglist", housing_id, "bathrooms", "3.5", 2, 1485409432,
    )
    assert value == 3
    assert backoffice_data.cell("housinglist", housing_id, "bathrooms")["value"] == 3

    value = backoffice_data.update_cell(
        "housinglist", housing_id, "bathrooms", "2+1", 3, 1485409432,
    )
    assert value == 2
    assert backoffice_data.cell("housinglist", housing_id, "bathrooms")["value"] == 2
