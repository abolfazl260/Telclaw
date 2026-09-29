"""Field-specific canonicalization for structured category data.

Raw Telegram source text is never changed here. Rules apply only to structured
category fields and are persisted in SQLite so Back Office users can manage them.
"""
from __future__ import annotations

import math
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
        ("housinglist", "listing_type", "for sale", "sale", "",
         "For-sale listing type normalized to sale"),
        ("housinglist", "listing_type", "sale", "sale", "",
         "Canonical sale listing type"),
        ("housinglist", "rent_period", "1 ŸÖÿßŸáŸá", "short_term", "",
         "Repair mojibake one-month rental duration"),
        ("housinglist", "rent_period", "1-6 months", "short_term", "",
         "Fixed limited rental duration"),
        ("housinglist", "rent_period", "2 months", "short_term", "",
         "Fixed limited rental duration"),
        ("housinglist", "rent_period", "3 months", "short_term", "",
         "Fixed limited rental duration"),
        ("housinglist", "rent_period", "6 months", "short_term", "",
         "Fixed limited rental duration"),
        ("housinglist", "rent_period", "6 months or less", "short_term", "",
         "Fixed limited rental duration"),
        ("housinglist", "rent_period", "daily", "daily", "",
         "Advertio canonical rental duration"),
        ("housinglist", "rent_period", "day", "daily", "",
         "Advertio daily rental duration"),
        ("housinglist", "rent_period", "per day", "daily", "",
         "Advertio daily rental duration"),
        ("housinglist", "rent_period", "month", "long_term", "",
         "Monthly rental normalized to Advertio long_term"),
        ("housinglist", "rent_period", "monthly", "long_term", "",
         "Monthly rental normalized to Advertio long_term"),
        ("housinglist", "rent_period", "per month", "long_term", "",
         "Monthly rental normalized to Advertio long_term"),
        ("housinglist", "rent_period", "Monthly / Short-Term", "short_term", "",
         "Explicit short-term rental duration"),
        ("housinglist", "rent_period", "Monthly, Short-Term", "short_term", "",
         "Explicit short-term rental duration"),
        ("housinglist", "rent_period", "one month", "short_term", "",
         "Explicit fixed one-month rental duration"),
        ("housinglist", "rent_period", "short-term", "short_term", "",
         "Advertio canonical short-term rental duration"),
        ("housinglist", "rent_period", "short term", "short_term", "",
         "Advertio canonical short-term rental duration"),
        ("housinglist", "rent_period", "short_term", "short_term", "",
         "Advertio canonical rental duration"),
        ("housinglist", "rent_period", "weekly", "short_term", "",
         "Weekly rental normalized to Advertio short_term"),
        ("housinglist", "rent_period", "per week", "short_term", "",
         "Weekly rental normalized to Advertio short_term"),
        ("housinglist", "rent_period", "weekly and monthly", "short_term", "",
         "Flexible weekly/monthly rental includes short-term availability"),
        ("housinglist", "rent_period", "year", "long_term", "",
         "Yearly rental normalized to Advertio long_term"),
        ("housinglist", "rent_period", "yearly", "long_term", "",
         "Yearly rental normalized to Advertio long_term"),
        ("housinglist", "rent_period", "long-term", "long_term", "",
         "Advertio canonical long-term rental duration"),
        ("housinglist", "rent_period", "long term", "long_term", "",
         "Advertio canonical long-term rental duration"),
        ("housinglist", "rent_period", "long_term", "long_term", "",
         "Advertio canonical rental duration"),
        ("housinglist", "bedrooms", "0", "0", "", "Advertio canonical bedroom value"),
        ("housinglist", "bedrooms", "1", "1", "", "Advertio canonical bedroom value"),
        ("housinglist", "bedrooms", "2", "2", "", "Advertio canonical bedroom value"),
        ("housinglist", "bedrooms", "3", "3", "", "Advertio canonical bedroom value"),
        ("housinglist", "bedrooms", "4", "4+", "", "Advertio groups four or more bedrooms as 4+"),
        ("housinglist", "bedrooms", "4+", "4+", "", "Advertio canonical bedroom value"),
        ("housinglist", "bedrooms", "5", "4+", "", "Advertio groups four or more bedrooms as 4+"),
        ("housinglist", "bedrooms", "6", "4+", "", "Advertio groups four or more bedrooms as 4+"),
        ("housinglist", "bedrooms", "one", "1", "", "English bedroom count"),
        ("housinglist", "bedrooms", "Bachelor", "0", "", "Bachelor/studio layout normalized to zero bedrooms"),
        ("housinglist", "bedrooms", "1+Den", "1", "", "One bedroom plus den"),
        ("housinglist", "bedrooms", "1+1", "1", "", "One bedroom plus den/extra room"),
        ("housinglist", "bedrooms", "2+1", "2", "", "Two bedrooms plus den/extra room"),
        ("housinglist", "bedrooms", "4+1", "4+", "", "Four or more bedrooms plus extra room"),
        ("housinglist", "bedrooms", "master", "1", "", "User-approved master-room mapping"),
        ("housinglist", "bedrooms", "single", "0", "", "User-approved single-room mapping"),
        ("housinglist", "bedrooms", "1 person or 1 couple", None, "", "Occupancy text, not a reliable bedroom count"),
        ("housinglist", "bedrooms", "single or double", "0", "", "User-approved room-type mapping"),
        ("housinglist", "bedrooms", "single/double", "0", "", "User-approved room-type mapping"),
        ("housinglist", "bedrooms", "single and double", None, "", "Multiple room types without reliable bedroom count"),
        ("housinglist", "bedrooms", "0, 1, 2, 3, 4+", "1", "", "User-approved multi-option bedroom mapping"),
        ("housinglist", "bedrooms", "1, 2, 3+", "1", "", "User-approved multi-option bedroom mapping"),
        ("housinglist", "bedrooms", "1, 2, 2+", "2", "", "User-approved multi-option bedroom mapping"),
        ("housinglist", "bedrooms", "1, 2, 3, 4+", "1", "", "User-approved multi-option bedroom mapping"),
        ("housinglist", "bedrooms", "1, 2, 3", "1", "", "User-approved multi-option bedroom mapping"),
        ("housinglist", "bedrooms", "2-Jan", "0", "", "User-approved corrupted bedroom mapping"),
        ("housinglist", "bedrooms", "2 or 1", "1", "", "User-approved ambiguous bedroom mapping"),
        ("housinglist", "bedrooms", "2 or 3", "2", "", "User-approved ambiguous bedroom mapping"),
        ("housinglist", "bedrooms", "1+", None, "", "Bedroom count is not exact enough for Advertio enum"),
        ("housinglist", "bedrooms", "2+", None, "", "Bedroom count is not exact enough for Advertio enum"),
        ("housinglist", "bedrooms", "3+", None, "", "Advertio has no exact 3+ bedroom enum"),
        ("housinglist", "bedrooms", "3-Jan", None, "", "Corrupted bedroom value; do not guess"),
        ("housinglist", "bedrooms", "5¬Ω", None, "", "Corrupted layout value; do not guess"),
        ("housinglist", "area_unit", "Sq.Ft", "sqft", "", "Canonical square-foot unit"),
        ("housinglist", "area_unit", "sqft", "sqft", "", "Canonical square-foot unit"),
        ("housinglist", "area_unit", "sqf", "sqft", "", "Square-foot unit alias"),
        ("housinglist", "area_unit", "sq ft", "sqft", "", "Square-foot unit alias"),
        ("housinglist", "area_unit", "square feet", "sqft", "", "Square-foot unit alias"),
        ("housinglist", "area_unit", "square foot", "sqft", "", "Square-foot unit alias"),
        ("housinglist", "area_unit", "sq feet", "sqft", "", "Square-foot unit alias"),
        ("housinglist", "area_unit", "foot", "sqft", "", "Area field foot unit treated as square feet"),
        ("housinglist", "area_unit", "sf", "sqft", "", "Square-foot unit alias"),
        ("housinglist", "area_unit", "ft¬≤", "sqft", "", "Repair mojibake square-foot unit"),
        ("housinglist", "area_unit", "ft²", "sqft", "", "Square-foot unit alias"),
        ("housinglist", "area_unit", "square meters", "sqm", "", "Canonical square-meter unit"),
        ("housinglist", "area_unit", "sqm", "sqm", "", "Canonical square-meter unit"),
        ("housinglist", "area_unit", "m¬≤", "sqm", "", "Repair mojibake square-meter unit"),
        ("housinglist", "area_unit", "m²", "sqm", "", "Square-meter unit alias"),
        ("housinglist", "area_unit", "meters", "sqm", "", "Area field meters unit treated as square meters"),
        ("housinglist", "furnished", "1", "furnished", "", "Canonical furnished value"),
        ("housinglist", "furnished", "yes", "furnished", "", "Affirmative furnished value"),
        ("housinglist", "furnished", "true", "furnished", "", "Boolean-like furnished value"),
        ("housinglist", "furnished", "furnished", "furnished", "", "Advertio canonical furnishing value"),
        ("housinglist", "furnished", "0", "unfurnished", "", "Canonical unfurnished value"),
        ("housinglist", "furnished", "no", "unfurnished", "", "Negative furnished value"),
        ("housinglist", "furnished", "false", "unfurnished", "", "Boolean-like unfurnished value"),
        ("housinglist", "furnished", "unfurnished", "unfurnished", "", "Advertio canonical furnishing value"),
        ("housinglist", "furnished", "partial", "partially", "", "Partially furnished alias"),
        ("housinglist", "furnished", "partially", "partially", "", "Advertio canonical furnishing value"),
        ("housinglist", "furnished", "partially furnished", "partially", "", "Partially furnished alias"),
        ("housinglist", "property_condition", "fully renovated", "renovated", "", "Canonical renovated condition"),
        ("housinglist", "property_condition", "renovated", "renovated", "", "Canonical renovated condition"),
        ("housinglist", "property_condition", "newly renovated", "renovated", "", "Canonical renovated condition"),
        ("housinglist", "property_condition", "recently renovated", "renovated", "", "Canonical renovated condition"),
        ("housinglist", "property_condition", "new", "new", "", "Canonical new condition"),
        ("housinglist", "property_condition", "newly built", "new", "", "New-build condition"),
        ("housinglist", "property_condition", "brand new", "new", "", "Canonical new condition"),
        ("housinglist", "property_condition", "almost new", "new", "", "Near-new condition normalized to new"),
        ("housinglist", "property_condition", "good", "good", "", "Canonical internal condition"),
        ("housinglist", "property_condition", "excellent", "excellent", "", "Canonical internal condition"),
        ("housinglist", "property_condition", "luxury", "luxury", "", "Preserve explicit quality descriptor internally"),
        ("housinglist", "property_condition", "modern", "modern", "", "Preserve explicit style/condition descriptor internally"),
        ("housinglist", "property_condition", "clean", "clean", "", "Canonical clean condition"),
        ("housinglist", "property_condition", "clean and organized", "clean", "", "Remove non-condition wording"),
        ("housinglist", "property_condition", "newly painted", "newly painted", "", "Preserve explicit maintenance condition"),
        ("housinglist", "property_condition", "Fully furnished, clean, ready to move in", "clean", "", "Keep condition only; furnishing/availability belong elsewhere"),
        ("housinglist", "property_condition", "ready for move-in", None, "", "Availability text, not property condition"),
        ("housinglist", "property_condition", "furnished, ready to move in", None, "", "Furnishing/availability text, not property condition"),
        ("housinglist", "property_condition", "New and resale", None, "", "Ambiguous mixed condition; do not guess"),
        ("housinglist", "province", "AB", "Alberta", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "BC", "British Columbia", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "MB", "Manitoba", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "NB", "New Brunswick", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "NL", "Newfoundland and Labrador", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "NS", "Nova Scotia", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "NT", "Northwest Territories", "CA", "Canadian territory code to full name"),
        ("housinglist", "province", "NU", "Nunavut", "CA", "Canadian territory code to full name"),
        ("housinglist", "province", "ON", "Ontario", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "PE", "Prince Edward Island", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "QC", "Quebec", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "Quebec", "Quebec", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Alberta", "Alberta", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "British Columbia", "British Columbia", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Manitoba", "Manitoba", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "New Brunswick", "New Brunswick", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Newfoundland and Labrador", "Newfoundland and Labrador", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Nova Scotia", "Nova Scotia", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Northwest Territories", "Northwest Territories", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Nunavut", "Nunavut", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Ontario", "Ontario", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Prince Edward Island", "Prince Edward Island", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Saskatchewan", "Saskatchewan", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "Yukon", "Yukon", "CA", "Canonical Canadian province name"),
        ("housinglist", "province", "SK", "Saskatchewan", "CA", "Canadian province code to full name"),
        ("housinglist", "province", "YT", "Yukon", "CA", "Canadian territory code to full name"),
        ("housinglist", "province", "Qu√©bec", "Quebec", "CA", "Repair mojibake province name"),
        ("housinglist", "province", "Lazio", None, "CA", "Invalid Canadian province; do not guess"),
        ("housinglist", "city", "Bernaby", "Burnaby", "CA", "Correct common city misspelling"),
        ("housinglist", "city", "C√¥te-Saint-Luc", "Côte-Saint-Luc", "CA", "Repair mojibake city name"),
        ("housinglist", "city", "Montr√©al", "Montreal", "CA", "Repair mojibake city name"),
        ("housinglist", "city", "Richmondhill", "Richmond Hill", "CA", "Canonical city spelling"),
        ("housinglist", "city", "Multiple", None, "CA", "Ambiguous multi-city value"),
        ("housinglist", "city", "Multiple cities", None, "CA", "Ambiguous multi-city value"),
        ("housinglist", "city", "RICHMOND HILL & AURORA", None, "CA", "Multiple cities; do not guess"),
        ("housinglist", "city", "Rome or Blonay", None, "CA", "Multiple/ambiguous cities; do not guess"),
        ("housinglist", "city", "Ram", None, "CA", "Ambiguous invalid Canadian city"),
        ("housinglist", "city", "Rim", None, "CA", "Ambiguous invalid Canadian city"),
        ("housinglist", "city", "Rum", None, "CA", "Ambiguous invalid Canadian city"),
        ("housinglist", "city", "Rumiyah", None, "CA", "Invalid Canadian city; do not guess"),
        ("housinglist", "city", "Lanzadel", None, "CA", "Unresolved city value; do not guess"),
        ("housinglist", "city", "Terrenee", None, "CA", "Unresolved city value; do not guess"),
        ("housinglist", "city", "Tehran (Rum)", None, "CA", "Ambiguous non-Canadian city value"),
        ("housinglist", "city", "Caserta", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Cookeville", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "D√ºsseldorf", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Düsseldorf", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Frankfurt", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "K√∂ln", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Köln", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Mashhad", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Roma", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Tehran", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Torino", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Turin", None, "CA", "Known non-Canadian city in Canadian housing scope"),
        ("housinglist", "city", "Lazio", None, "CA", "Region/non-city value in Canadian housing scope"),
        ("housinglist", "neighborhood", "Bathurs", "Bathurst", "CA", "Correct neighborhood/street spelling"),
        ("housinglist", "neighborhood", "Cookeilam", "Coquitlam", "CA", "Correct common Coquitlam misspelling"),
        ("housinglist", "neighborhood", "Bayview and Sheppard", "Bayview & Sheppard", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Bayview and Major McKenzie Dr", "Bayview & Major Mackenzie", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Marine Dr", "Marine Drive", "CA", "Canonical street spelling"),
        ("housinglist", "neighborhood", "Marin drive", "Marine Drive", "CA", "Correct street misspelling"),
        ("housinglist", "neighborhood", "Finch/Yong", "Yonge & Finch", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Finch & Yonge", "Yonge & Finch", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge / Finch", "Yonge & Finch", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge and Finch", "Yonge & Finch", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge&Finch", "Yonge & Finch", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Young and Finch", "Yonge & Finch", "CA", "Correct Yonge spelling"),
        ("housinglist", "neighborhood", "Yang and Finch", "Yonge & Finch", "CA", "Correct Yonge spelling"),
        ("housinglist", "neighborhood", "Finnch and Bathurst", "Finch & Bathurst", "CA", "Correct Finch spelling"),
        ("housinglist", "neighborhood", "Finch and Bathurst", "Finch & Bathurst", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Finch and Bath", "Finch & Bathurst", "CA", "Canonical Bathurst intersection spelling"),
        ("housinglist", "neighborhood", "Yonge/Sheppard", "Yonge & Sheppard", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge and Sheppard", "Yonge & Sheppard", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Near Yonge and Sheppard", "Yonge & Sheppard", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge / Steeles", "Yonge & Steeles", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge and Steeles", "Yonge & Steeles", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge steeles", "Yonge & Steeles", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge & Steels", "Yonge & Steeles", "CA", "Correct Steeles spelling"),
        ("housinglist", "neighborhood", "Yonge & Elginmills", "Yonge & Elgin Mills", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Yonge and Elgin Mills", "Yonge & Elgin Mills", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Steeles &Bayview", "Bayview & Steeles", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Sheppard & Donmills", "Don Mills & Sheppard", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Shepherd and 404", "Sheppard & Hwy 404", "CA", "Correct Sheppard spelling"),
        ("housinglist", "neighborhood", "Victoria Park and Shepherd", "Victoria Park & Sheppard", "CA", "Correct Sheppard spelling"),
        ("housinglist", "neighborhood", "Weston Rd and black creek", "Weston Road & Black Creek", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Weston Road and Black Creek", "Weston Road & Black Creek", "CA", "Canonical intersection spelling"),
        ("housinglist", "neighborhood", "Upper Lansdale", "Upper Lonsdale", "CA", "Correct Lonsdale spelling"),
        ("housinglist", "neighborhood", "Multiple", None, "CA", "Ambiguous neighborhood value"),
        ("housinglist", "neighborhood", "Multiple neighborhoods", None, "CA", "Ambiguous neighborhood value"),
        ("housinglist", "neighborhood", "Various", None, "CA", "Ambiguous neighborhood value"),
        ("housinglist", "neighborhood", "A metro area", None, "CA", "Generic non-neighborhood value"),
        ("housinglist", "neighborhood", "C/A/B metro area", None, "CA", "Ambiguous generic area"),
        ("housinglist", "neighborhood", "center", None, "CA", "Generic non-neighborhood value"),
        ("housinglist", "neighborhood", "central area", None, "CA", "Generic non-neighborhood value"),
        ("housinglist", "neighborhood", "Metropolitan A", None, "CA", "Generic non-neighborhood value"),
        ("housinglist", "neighborhood", "Lower", None, "CA", "Incomplete non-neighborhood value"),
        ("housinglist", "neighborhood", "LOWER - 328 MOORE PARK AVENUE", None, "CA", "Property/address fragment, not neighborhood"),
        ("housinglist", "neighborhood", "Regions 14, 3, 2, 15", None, "CA", "Multiple regions, not one neighborhood"),
        ("housinglist", "neighborhood", "Rum", None, "CA", "Invalid/ambiguous neighborhood value"),
        ("housinglist", "neighborhood", '["Dun Tan","North   York","Thornhill","Richmond   Hill","Aurora","Newmarket"]', None, "CA", "Multiple locations in one field"),
        ("housinglist", "neighborhood", "North Vancouver, Coquitlam, West Vancouver, Port Coquitlam, Downtown, Burnaby", None, "CA", "Multiple cities in one neighborhood field"),
        ("housinglist", "neighborhood", "North York, Downtown Toronto, Midtown Toronto, Richmond Hill, Vaughan, Markham, Aurora, Newmarket, Scarborough, Etobicoke", None, "CA", "Multiple cities/neighborhoods in one field"),
        ("housinglist", "neighborhood", "North York, Richmond Hill, Markham, Vaughan, Newmarket", None, "CA", "Multiple cities in one field"),
        ("housinglist", "neighborhood", "North York, Richmond Hill, Markham, Vaughan, Newmarket, GTA", None, "CA", "Multiple cities in one field"),
        ("housinglist", "neighborhood", "Vancouver, Burnaby, New Westminster", None, "CA", "Multiple cities in one field"),
        ("housinglist", "neighborhood", "ÿ¢ÿ±Ÿàÿ±ÿß", None, "CA", "Corrupted text"),
        ("housinglist", "neighborhood", "ÿßÿ±Ÿàÿ±ÿß", None, "CA", "Corrupted text"),
        ("housinglist", "neighborhood", "ŸÜŸàÿ±ÿ™ €åŸàÿ±⁄©", None, "CA", "Corrupted text"),
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

_AREA_SQFT_KEYS = {
    _alias_key(value) for value in (
        "Sq.Ft", "sqft", "sqf", "sq ft", "square feet", "square foot",
        "sq feet", "foot", "sf", "ft¬≤", "ft²",
    )
}
_AREA_SQM_KEYS = {
    _alias_key(value) for value in (
        "square meters", "sqm", "m¬≤", "m²", "meters",
    )
}

_AVAILABILITY_TO_RENT_PERIOD = {
    "daily": "daily",
    "short-term": "short_term",
    "short term": "short_term",
    "short_term": "short_term",
    "temporary": "short_term",
    "monthly / short-term": "short_term",
    "monthly / short term": "short_term",
    "1-6 months": "short_term",
    "short-term and monthly": "short_term",
    "short-term, monthly": "short_term",
    "short-term or long-term": "short_term",
    "short term & long term": "short_term",
    "short term &long term": "short_term",
    "short and long term": "short_term",
    "short-term and long-term": "short_term",
    "long-term and short-term (minimum 3 months)": "short_term",
    "short and medium term": "short_term",
    "temporary or permanent": "short_term",
    "long-term": "long_term",
    "long term": "long_term",
    "one year": "long_term",
    "no short-term rentals": "long_term",
}

_AMBIGUOUS_AVAILABILITY = {
    "available",
    "available now",
    "immediately",
    "immediate",
    "ready to move in",
    "september",
    "october",
    "available october",
    "early november",
    "mid-october",
    "available october first",
    "from 1 november",
    "from 6 october",
    "ready to rent from september 1",
    "available from mid-september",
    "october 1 or november 1",
    "available: november 1st",
    "november 1st",
    "15-sep",
    "1-sep",
    "21-sep",
    "10-oct",
    "september‚äø1",
    "september 5 to november 5",
    "available: about 25 sep ‚äì 10 nov",
    "18 september to 30 november",
    "until december 1st",
}


def availability_rent_period(value):
    """Return canonical rent_period when availability actually contains duration semantics."""
    if value is None:
        return None
    return _AVAILABILITY_TO_RENT_PERIOD.get(str(value).strip().casefold())


def normalize_housing_availability(value):
    """Return Advertio-compatible YYYY-MM-DD or None for vague/non-date availability."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.casefold() in _NULL_LIKE_TEXT:
        return None

    key = text.casefold()
    if key in _AVAILABILITY_TO_RENT_PERIOD or key in _AMBIGUOUS_AVAILABILITY:
        return None

    # ISO date or ISO range: Advertio wants the first available date.
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+to\s+\d{4}-\d{2}-\d{2})?$", text, re.I)
    if match:
        candidate = match.group(1)
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return None

    # US/Canadian numeric date as present in the dataset: M/D/YY or M/D/YYYY.
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", text)
    if match:
        month, day, year = map(int, match.groups())
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # Explicit English dates with a year.
    for fmt in ("%b %d,%Y", "%b %d, %Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Date ranges where the first date has an explicit year.
    match = re.match(
        r"^([A-Za-z]{3,9}\s+\d{1,2}\s+\d{4})\s+to\s+[A-Za-z]{3,9}\s+\d{1,2}\s+\d{4}$",
        text,
        re.I,
    )
    if match:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(match.group(1), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # "after YYYY-MM-DD" is not an exact available_from date, so do not guess.
    if re.fullmatch(r"after\s+\d{4}-\d{2}-\d{2}", text, re.I):
        return None

    return None


def normalize_housing_area(area, area_unit):
    """Return (area, unit) with housing area expressed in square metres when possible."""
    if area_unit is None or str(area_unit).strip().casefold() in _NULL_LIKE_TEXT:
        return area, None

    unit_key = _alias_key(area_unit)
    if unit_key in _AREA_SQFT_KEYS:
        canonical_unit = "sqft"
    elif unit_key in _AREA_SQM_KEYS:
        canonical_unit = "sqm"
    else:
        return area, area_unit

    if area is None or isinstance(area, bool):
        return area, canonical_unit
    try:
        number = float(area)
    except (TypeError, ValueError):
        return area, canonical_unit
    if not math.isfinite(number):
        return area, canonical_unit

    if canonical_unit == "sqft":
        number = round(number * 0.09290304, 2)
        canonical_unit = "sqm"
    return number, canonical_unit


def normalize_builtin_field_value(category, field_name, value):
    """Apply deterministic non-alias normalization for typed structured fields."""
    if category == "housinglist" and field_name == "availability":
        return normalize_housing_availability(value)

    if category == "housinglist" and field_name == "furnished":
        if value is None:
            return None
        if value is True:
            return "furnished"
        if value is False:
            return "unfurnished"
        text = str(value).strip()
        if text.casefold() in _NULL_LIKE_TEXT:
            return None
        return value

    if category == "housinglist" and field_name == "bathrooms":
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip()
        if text.casefold() in _NULL_LIKE_TEXT:
            return None
        # Values like "2+1" mean two full bathrooms plus an extra partial/secondary
        # bathroom in this dataset. The product policy is to round the bathroom
        # count down, so only the first numeric component is relevant.
        match = re.match(r"^([+-]?\d+(?:\.\d+)?)", text)
        if not match:
            return None
        try:
            number = float(match.group(1))
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        rounded = math.floor(number)
        # Advertio accepts bathroom counts from 1 through 10.
        if rounded < 1 or rounded > 10:
            return None
        return rounded
    return value


def normalize_structured_field_value(category, field_name, value, row_data=None):
    """Normalize one structured field, including typed built-ins and alias rules."""
    if category not in database.CATEGORY_TABLES or field_name not in database.CATEGORY_TABLES[category]:
        raise ValueError("Unknown normalization field")
    data = dict(row_data or {})
    data[field_name] = value
    normalized = normalize_builtin_field_value(category, field_name, value)
    if field_name not in aliasable_fields().get(category, ()):
        return normalized

    initialize()
    conn = database.get_connection()
    try:
        rules = _rules_for_category(conn, category)
        data[field_name] = normalized
        return _normalize_with_rules(category, field_name, normalized, data, rules)
    finally:
        conn.close()


_HOUSING_CITY_BY_PROVINCE = {
    ("Ontario", "Don Mills"): "Toronto",
    ("Ontario", "Don Valley"): "Toronto",
    ("Ontario", "East Woodbridge"): "Vaughan",
    ("Ontario", "Maple"): "Vaughan",
    ("Ontario", "North York"): "Toronto",
    ("Ontario", "Oak Ridges"): "Richmond Hill",
    ("Ontario", "Yonge / Finch"): "Toronto",
    ("British Columbia", "North Burnaby"): "Burnaby",
    ("British Columbia", "Yaletown"): "Vancouver",
}

_HOUSING_NEIGHBORHOOD_BY_LOCATION = {
    ("British Columbia", "North Vancouver", "North Vancouver - Delbrook"): "Delbrook",
    ("British Columbia", "North Vancouver", "North Vancouver - Upper Lonsdale"): "Upper Lonsdale",
    ("British Columbia", "West Vancouver", "West Vancouver Ambleside"): "Ambleside",
    ("British Columbia", "Langley", "Brookswood, Langley"): "Brookswood",
    ("Ontario", "Richmond Hill", "Richmond Hill - West Brook"): "Westbrook",
    ("Ontario", "Richmond Hill", "Oak Ridges Richmond Hill"): "Oak Ridges",
    ("Ontario", "Toronto", "Midtown (Yonge and Eglinton)"): "Midtown",
}


def _normalize_contextual_value(category, field_name, value, data):
    if category != "housinglist":
        return value
    country = str(data.get("country_code") or "").strip().upper()
    if country != "CA":
        return value

    text = str(value or "").strip()
    key = _alias_key(text)

    if field_name == "city":
        if key.startswith("rum ("):
            return None
        province = str(data.get("province") or "").strip()
        for (expected_province, alias), canonical in _HOUSING_CITY_BY_PROVINCE.items():
            if _alias_key(province) == _alias_key(expected_province) and key == _alias_key(alias):
                return canonical
        return value

    if field_name == "neighborhood":
        province = str(data.get("province") or "").strip()
        city = str(data.get("city") or "").strip()
        for (expected_province, expected_city, alias), canonical in _HOUSING_NEIGHBORHOOD_BY_LOCATION.items():
            if (
                _alias_key(province) == _alias_key(expected_province)
                and _alias_key(city) == _alias_key(expected_city)
                and key == _alias_key(alias)
            ):
                return canonical
    return value


def _normalize_with_rules(category, field_name, value, data, rules):
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return value
    if str(value).strip().casefold() in _NULL_LIKE_TEXT:
        return None
    contextual = _normalize_contextual_value(category, field_name, value, data)
    if contextual != value:
        return contextual
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

        if category == "housinglist" and "availability" in result:
            duration = availability_rent_period(result.get("availability"))
            if duration and not result.get("rent_period"):
                before_rent = result.get("rent_period")
                result["rent_period"] = duration
                changes["rent_period"] = (before_rent, duration)

        if category == "housinglist" and ("area" in result or "area_unit" in result):
            before_area = result.get("area")
            before_unit = result.get("area_unit")
            canonical_unit = _normalize_with_rules(
                category, "area_unit", before_unit, result, rules
            ) if "area_unit" in result else None
            normalized_area, normalized_unit = normalize_housing_area(before_area, canonical_unit)

            if "area" in result and normalized_area != before_area:
                result["area"] = normalized_area
                changes["area"] = (before_area, normalized_area)
            if "area_unit" in result and normalized_unit != before_unit:
                result["area_unit"] = normalized_unit
                changes["area_unit"] = (before_unit, normalized_unit)

        # Normalize country-ish fields first so country-scoped city aliases can match.
        fields = list(database.CATEGORY_TABLES[category])
        fields.sort(key=lambda name: 0 if name.endswith("_country") or name == "country_code" else 1)
        for field_name in fields:
            if field_name not in result:
                continue
            before = result.get(field_name)
            built_in = normalize_builtin_field_value(category, field_name, before)
            after = _normalize_with_rules(category, field_name, built_in, result, rules)
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
        built_in = normalize_builtin_field_value(category, field_name, value)
        proposed[field_name] = built_in
        return _normalize_with_rules(category, field_name, built_in, proposed, rules)
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
