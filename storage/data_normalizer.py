"""Field-specific canonicalization for structured category data.

Raw Telegram source text is never changed here. Rules apply only to structured
category fields and are persisted in SQLite so Back Office users can manage them.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from storage import database
from storage.location_normalizer import normalize_location


def _alias_key(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def aliasable_fields():
    """Return text fields that can safely use exact alias->canonical rules."""
    return {
        category: tuple(
            field for field, definition in fields.items()
            if str(definition).upper().startswith("TEXT")
        )
        for category, fields in database.CATEGORY_TABLES.items()
    }


def _validate_target(category, field_name):
    fields = aliasable_fields()
    if category not in fields:
        raise ValueError("Unknown normalization category")
    if field_name not in fields[category]:
        raise ValueError("Only structured TEXT fields can use normalization aliases")


def _country_field(category, field_name):
    if category == "transferlist":
        if field_name.startswith("origin_"):
            return "origin_country"
        if field_name.startswith("destination_"):
            return "destination_country"
    if category == "housinglist" and field_name in {"city", "province", "neighborhood"}:
        return "country_code"
    return None


def initialize():
    conn = database.get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS normalization_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            field_name TEXT NOT NULL,
            alias TEXT NOT NULL,
            alias_key TEXT NOT NULL,
            canonical_value TEXT,
            country_iso2 TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(category, field_name, alias_key, country_iso2)
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_normalization_alias_lookup
            ON normalization_aliases(category, field_name, alias_key, country_iso2, enabled)""")
        _seed_defaults(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_defaults(conn):
    now = datetime.now(timezone.utc).isoformat()
    defaults = []
    for field in ("origin_city", "destination_city"):
        defaults.extend([
            ("transferlist", field, "Dusseldorf", "Düsseldorf", "", "Canonical city spelling"),
            ("transferlist", field, "Duesseldorf", "Düsseldorf", "", "Canonical city spelling"),
            ("transferlist", field, "Frankfurt (Main)", "Frankfurt", "", "Canonical city name"),
            ("transferlist", field, "Frankfurt am Main", "Frankfurt", "", "Canonical city name"),
            ("transferlist", field, "Frankfurt/Main", "Frankfurt", "", "Canonical city name"),
            ("transferlist", field, "LA", "Los Angeles", "", "Common city abbreviation"),
            ("transferlist", field, "Imam Airport", "Tehran", "", "Airport reference normalized to city"),
            ("transferlist", field, "Imam Khomeini Airport", "Tehran", "", "Airport reference normalized to city"),
            ("transferlist", field, "Imam Khomeini International Airport", "Tehran", "", "Airport reference normalized to city"),
            ("transferlist", field, "Dusseldorf/Wuppertal", None, "", "Ambiguous multiple-city value"),
        ])
    defaults.extend([
        ("housinglist", "property_type", "condo, house, townhouse, basement", "apartment", "",
         "Multiple property types normalized to apartment"),
        ("housinglist", "property_type", "apartment, condo, house, townhouse, basement", "apartment", "",
         "Multiple property types normalized to multi"),
        ("housinglist", "property_type", '["condo","house","townhouse","basement"]', "apartment", "",
         "JSON property-type list normalized to apartment"),
        ("housinglist", "property_type", "multi", "apartment", "",
         "Generic multi property type normalized to apartment"),
        ("housinglist", "property_type", "townhouse", "apartment", "",
         "Townhouse normalized to apartment"),
    ])
    for category, field_name, alias, canonical, country, notes in defaults:
        conn.execute("""INSERT OR IGNORE INTO normalization_aliases(
            category,field_name,alias,alias_key,canonical_value,country_iso2,
            enabled,notes,created_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,1,?,NULL,?,?)""",
        (category, field_name, alias, _alias_key(alias), canonical, country, notes, now, now))


def list_aliases():
    initialize()
    conn = database.get_connection()
    try:
        return [dict(row) for row in conn.execute(
            """SELECT * FROM normalization_aliases
               ORDER BY category, field_name, alias COLLATE NOCASE, id"""
        ).fetchall()]
    finally:
        conn.close()


def get_alias(alias_id):
    initialize()
    conn = database.get_connection()
    try:
        row = conn.execute("SELECT * FROM normalization_aliases WHERE id=?", (int(alias_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_alias(category, field_name, alias, canonical_value, *, country_iso2="",
               enabled=True, notes="", admin_id=None, alias_id=None, map_to_null=False):
    initialize()
    category = str(category or "").strip()
    field_name = str(field_name or "").strip()
    _validate_target(category, field_name)

    alias = str(alias or "").strip()
    if not alias:
        raise ValueError("Alias is required")
    alias_key = _alias_key(alias)
    if not alias_key:
        raise ValueError("Alias is invalid")

    country = str(country_iso2 or "").strip().upper()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Country scope must be an ISO 3166-1 alpha-2 code")

    canonical = None if map_to_null else str(canonical_value or "").strip()
    if not map_to_null and not canonical:
        raise ValueError("Canonical value is required unless Map to NULL is selected")

    now = datetime.now(timezone.utc).isoformat()
    conn = database.get_connection()
    try:
        if alias_id:
            alias_id = int(alias_id)
            cursor = conn.execute("""UPDATE normalization_aliases
                SET category=?,field_name=?,alias=?,alias_key=?,canonical_value=?,
                    country_iso2=?,enabled=?,notes=?,updated_at=?
                WHERE id=?""",
                (category, field_name, alias, alias_key, canonical, country,
                 int(bool(enabled)), str(notes or "").strip()[:1000], now, alias_id))
            if cursor.rowcount != 1:
                raise ValueError("Normalization alias not found")
        else:
            conn.execute("""INSERT INTO normalization_aliases(
                category,field_name,alias,alias_key,canonical_value,country_iso2,
                enabled,notes,created_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (category, field_name, alias, alias_key, canonical, country,
                 int(bool(enabled)), str(notes or "").strip()[:1000],
                 int(admin_id) if admin_id is not None else None, now, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_alias(alias_id):
    initialize()
    conn = database.get_connection()
    try:
        cursor = conn.execute("DELETE FROM normalization_aliases WHERE id=?", (int(alias_id),))
        conn.commit()
        if cursor.rowcount != 1:
            raise ValueError("Normalization alias not found")
    finally:
        conn.close()


def _rules_for_category(conn, category):
    return [dict(row) for row in conn.execute(
        """SELECT * FROM normalization_aliases
           WHERE category=? AND enabled=1
           ORDER BY CASE WHEN country_iso2<>'' THEN 0 ELSE 1 END, id""",
        (category,)).fetchall()]


def _country_matches(rule, category, field_name, data):
    scope = str(rule.get("country_iso2") or "").strip().upper()
    if not scope:
        return True
    country_field = _country_field(category, field_name)
    if not country_field:
        return False
    current = str(data.get(country_field) or "").strip().upper()
    return current == scope


_NULL_LIKE_TEXT = {"", "null", "none", "n/a", "na", "unknown", "not provided", "-"}


def _normalize_with_rules(category, field_name, value, data, rules):
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return value
    if str(value).strip().casefold() in _NULL_LIKE_TEXT:
        return None
    key = _alias_key(value)
    if not key:
        return value
    for rule in rules:
        if rule["field_name"] != field_name or rule["alias_key"] != key:
            continue
        if not _country_matches(rule, category, field_name, data):
            continue
        return rule["canonical_value"]
    return value


def normalize_category_data(category, data, *, conn=None):
    """Return a canonicalized copy plus {field: (before, after)} changes."""
    if not isinstance(data, dict):
        return data, {}
    if category not in database.CATEGORY_TABLES:
        return dict(data), {}

    owns_conn = conn is None
    if owns_conn:
        initialize()
        conn = database.get_connection()
    try:
        rules = _rules_for_category(conn, category)
        result = dict(data)
        changes = {}

        # Normalize country-ish fields first so country-scoped city aliases can match.
        fields = list(database.CATEGORY_TABLES[category])
        fields.sort(key=lambda name: 0 if name.endswith("_country") or name == "country_code" else 1)
        for field_name in fields:
            if field_name not in result:
                continue
            before = result.get(field_name)
            after = _normalize_with_rules(category, field_name, before, result, rules)
            if after != before:
                result[field_name] = after
                changes[field_name] = (before, after)
        return result, changes
    finally:
        if owns_conn:
            conn.close()


def normalize_field_value(category, field_name, value, row_data):
    _validate_target(category, field_name)
    initialize()
    conn = database.get_connection()
    try:
        rules = _rules_for_category(conn, category)
        proposed = dict(row_data or {})
        proposed[field_name] = value
        return _normalize_with_rules(category, field_name, value, proposed, rules)
    finally:
        conn.close()


def _sync_transfer_location(conn, row):
    conn.execute("""CREATE TABLE IF NOT EXISTS transfer_locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processed_message_id INTEGER NOT NULL UNIQUE,
        origin_city_canonical TEXT,
        origin_city_key TEXT,
        origin_country_iso2 TEXT,
        destination_city_canonical TEXT,
        destination_city_key TEXT,
        destination_country_iso2 TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE
    )""")
    origin = normalize_location(row.get("origin_city"), row.get("origin_country"))
    destination = normalize_location(row.get("destination_city"), row.get("destination_country"))
    conn.execute("""INSERT INTO transfer_locations(
        processed_message_id,
        origin_city_canonical, origin_city_key, origin_country_iso2,
        destination_city_canonical, destination_city_key, destination_country_iso2
    ) VALUES(?,?,?,?,?,?,?)
    ON CONFLICT(processed_message_id) DO UPDATE SET
        origin_city_canonical=excluded.origin_city_canonical,
        origin_city_key=excluded.origin_city_key,
        origin_country_iso2=excluded.origin_country_iso2,
        destination_city_canonical=excluded.destination_city_canonical,
        destination_city_key=excluded.destination_city_key,
        destination_country_iso2=excluded.destination_country_iso2""", (
        row.get("processed_message_id"),
        origin["city"], origin["city_key"], origin["country_iso2"],
        destination["city"], destination["city_key"], destination["country_iso2"],
    ))


def renormalize_existing(category=None):
    """Apply current aliases to existing structured rows. Published Telegram posts are untouched."""
    initialize()
    categories = [category] if category else list(database.CATEGORY_TABLES)
    for item in categories:
        if item not in database.CATEGORY_TABLES:
            raise ValueError("Unknown normalization category")

    conn = database.get_connection()
    totals = {"rows_scanned": 0, "rows_changed": 0, "cells_changed": 0}
    try:
        for item in categories:
            rows = conn.execute(f"SELECT * FROM {item}").fetchall()
            for sqlite_row in rows:
                row = dict(sqlite_row)
                totals["rows_scanned"] += 1
                normalized, changes = normalize_category_data(item, row, conn=conn)
                if not changes:
                    continue
                assignments = ", ".join(f'"{field}"=?' for field in changes)
                values = [normalized[field] for field in changes] + [row["id"]]
                conn.execute(f'UPDATE {item} SET {assignments} WHERE id=?', values)
                totals["rows_changed"] += 1
                totals["cells_changed"] += len(changes)
                if item == "transferlist":
                    synced = dict(row)
                    synced.update({field: normalized[field] for field in changes})
                    _sync_transfer_location(conn, synced)
        conn.commit()
        return totals
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def count_current_matches(alias_id):
    """Count current DB values that match one rule before a backfill."""
    rule = get_alias(alias_id)
    if not rule:
        raise ValueError("Normalization alias not found")
    category, field_name = rule["category"], rule["field_name"]
    _validate_target(category, field_name)
    country_field = _country_field(category, field_name)
    columns = f'id, "{field_name}"'
    if country_field:
        columns += f', "{country_field}"'
    conn = database.get_connection()
    try:
        rows = conn.execute(f"SELECT {columns} FROM {category}").fetchall()
        count = 0
        for sqlite_row in rows:
            row = dict(sqlite_row)
            if _alias_key(row.get(field_name)) != rule["alias_key"]:
                continue
            if not _country_matches(rule, category, field_name, row):
                continue
            current = row.get(field_name)
            if current != rule["canonical_value"]:
                count += 1
        return count
    finally:
        conn.close()
