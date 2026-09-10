"""Storage-safe normalization for AI-extracted locations.

The AI extraction prompt is responsible for converting multilingual city names
to a canonical international city name and for returning ISO 3166-1 alpha-2
country codes. This module does not maintain a city alias database or infer
locations; it only validates the AI country code and creates a deterministic
ASCII-safe reporting key.
"""
from __future__ import annotations

import re
import unicodedata


def country_iso2(value) -> str | None:
    """Accept only an AI-provided ISO 3166-1 alpha-2 country code."""
    text = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", text):
        return text
    return None


def _ascii_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text).strip("_").upper()
    return ascii_text


def canonical_city(value) -> str | None:
    """Return the canonical city exactly as supplied by the AI extractor."""
    text = str(value or "").strip()
    return text or None


def normalize_location(city, country):
    """Create a stable reporting representation without guessing location data."""
    canonical = canonical_city(city)
    iso2 = country_iso2(country)
    if not canonical:
        return {"city": None, "country_iso2": iso2, "city_key": None}
    key = _ascii_key(canonical)
    city_key = f"{iso2}_{key}" if iso2 else key
    return {
        "city": canonical,
        "country_iso2": iso2,
        "city_key": city_key or None,
    }
