"""Canonical location normalization for reporting.

Country identity follows ISO 3166-1 alpha-2. City identity is stored as a
stable ASCII reporting key built from the canonical English city name and ISO
country code. This avoids locale-specific spelling and special characters.
"""
from __future__ import annotations

import re
import unicodedata

try:
    import pycountry
except ImportError:  # pragma: no cover - dependency is declared in requirements
    pycountry = None

_COUNTRY_ALIASES = {
    "iran": "IR", "islamic republic of iran": "IR", "ایران": "IR", "ایران اسلامی": "IR",
    "germany": "DE", "deutschland": "DE", "آلمان": "DE",
    "canada": "CA", "کانادا": "CA",
    "turkey": "TR", "türkiye": "TR", "turkiye": "TR", "ترکیه": "TR",
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US", "america": "US", "آمریکا": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB", "بریتانیا": "GB", "انگلستان": "GB",
    "france": "FR", "فرانسه": "FR", "italy": "IT", "ایتالیا": "IT", "spain": "ES", "اسپانیا": "ES",
    "netherlands": "NL", "the netherlands": "NL", "هلند": "NL", "belgium": "BE", "بلژیک": "BE",
    "austria": "AT", "اتریش": "AT", "switzerland": "CH", "سوئیس": "CH", "sweden": "SE", "سوئد": "SE",
    "norway": "NO", "نروژ": "NO", "denmark": "DK", "دانمارک": "DK", "finland": "FI", "فنلاند": "FI",
    "poland": "PL", "لهستان": "PL", "greece": "GR", "یونان": "GR", "russia": "RU", "روسیه": "RU",
    "ukraine": "UA", "اوکراین": "UA", "united arab emirates": "AE", "uae": "AE", "امارات": "AE",
    "qatar": "QA", "قطر": "QA", "saudi arabia": "SA", "عربستان": "SA", "kuwait": "KW", "کویت": "KW",
    "oman": "OM", "عمان": "OM", "iraq": "IQ", "عراق": "IQ", "azerbaijan": "AZ", "آذربایجان": "AZ",
    "georgia": "GE", "گرجستان": "GE", "armenia": "AM", "ارمنستان": "AM", "china": "CN", "چین": "CN",
    "japan": "JP", "ژاپن": "JP", "south korea": "KR", "republic of korea": "KR", "کره جنوبی": "KR",
    "india": "IN", "هند": "IN", "pakistan": "PK", "پاکستان": "PK", "afghanistan": "AF", "افغانستان": "AF",
}

_CITY_ALIASES = {
    "hannover": "Hannover", "hanover": "Hannover", "هانوفر": "Hannover", "هانوور": "Hannover",
    "tehran": "Tehran", "teheran": "Tehran", "تهران": "Tehran", "طهران": "Tehran",
    "istanbul": "Istanbul", "constantinople": "Istanbul", "استانبول": "Istanbul",
    "berlin": "Berlin", "برلین": "Berlin", "munich": "Munich", "münchen": "Munich", "مونیخ": "Munich",
    "frankfurt": "Frankfurt", "فرانکفورت": "Frankfurt", "hamburg": "Hamburg", "هامبورگ": "Hamburg",
    "toronto": "Toronto", "تورنتو": "Toronto", "vancouver": "Vancouver", "ونکوور": "Vancouver",
    "montreal": "Montreal", "مونترال": "Montreal", "calgary": "Calgary", "کلگری": "Calgary",
    "london": "London", "لندن": "London", "paris": "Paris", "پاریس": "Paris",
    "milan": "Milan", "milano": "Milan", "میلان": "Milan", "rome": "Rome", "roma": "Rome", "رم": "Rome",
    "dubai": "Dubai", "دبی": "Dubai", "abu dhabi": "Abu Dhabi", "ابوظبی": "Abu Dhabi",
}


def country_iso2(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    key = text.casefold()
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    if re.fullmatch(r"[A-Za-z]{2}", text):
        code = text.upper()
        if pycountry and pycountry.countries.get(alpha_2=code):
            return code
    if pycountry:
        try:
            country = pycountry.countries.lookup(text)
            return country.alpha_2
        except LookupError:
            pass
    return None


def _ascii_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text).strip("_").upper()
    return ascii_text


def canonical_city(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    alias = _CITY_ALIASES.get(text.casefold())
    return alias or text


def normalize_location(city, country):
    """Return canonical display city, ISO2 country and stable ASCII key."""
    canonical = canonical_city(city)
    iso2 = country_iso2(country)
    if not canonical:
        return {"city": None, "country_iso2": iso2, "city_key": None, "unlocode": None}
    key = _ascii_key(canonical)
    city_key = f"{iso2}_{key}" if iso2 else key
    return {
        "city": canonical,
        "country_iso2": iso2,
        "city_key": city_key or None,
        # UN/LOCODE is intentionally nullable until a verified authoritative
        # location-code dataset is available; never let the AI invent it.
        "unlocode": None,
    }
