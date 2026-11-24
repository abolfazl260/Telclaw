"""Host-scoped, restart-safe snapshot of Advertio's passenger cargo routes."""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import config
from delivery.advertio_client import AdvertioError
from storage.database import get_connection
from storage.transfer_city_aliases import TRANSFER_CITY_ALIASES, TRANSFER_COUNTRY_ALIASES

CITY_LIMIT = 200
TTL = timedelta(hours=24)
RETRY = timedelta(minutes=15)
# Historical scope is retained only for moderation/audit, never queue admission.
PREVIOUS_COUNTRIES = frozenset(("IR", "CA", "US", "DE", "IT", "GB", "FR"))
ROUTE_EVIDENCE_SQL = "COALESCE(NULLIF(TRIM(m.raw_text),''),NULLIF(TRIM(m.text),''),m.cleaned_text)"
TRIP_WINDOW_SQL = """(
    ((t.departure_date IS NULL OR TRIM(t.departure_date)='') AND substr(m.date,1,10)>=?)
    OR (t.departure_date IS NOT NULL AND TRIM(t.departure_date)<>'' AND date(substr(t.departure_date,1,10))>=date(?))
)"""
UNREVIEWED_HISTORY_SQL = """
    advertio_cargo_history_scope(m.id,t.origin_country,t.destination_country)=1
    AND NOT EXISTS (SELECT 1 FROM advertio_cargo_history_review review WHERE review.local_id=m.id)
"""


def now():
    return datetime.now(timezone.utc)


def normalize(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.translate(str.maketrans({"ي": "ی", "ك": "ک", "‌": " ", "-": " ", "_": " "}))
    return " ".join(text.split())


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS advertio_cargo_catalog (
            host TEXT PRIMARY KEY, payload TEXT, synced_at TEXT,
            next_refresh TEXT NOT NULL, owner TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS advertio_cargo_history_review (
            local_id INTEGER PRIMARY KEY, reviewed_at TEXT NOT NULL, reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS advertio_cargo_history_boundary(
            id INTEGER PRIMARY KEY CHECK(id=1), max_message_id INTEGER NOT NULL
        );
    """)
    if not conn.execute("SELECT 1 FROM advertio_cargo_history_boundary WHERE id=1").fetchone():
        conn.execute("""
            INSERT OR IGNORE INTO advertio_cargo_history_boundary(id,max_message_id)
            SELECT 1,COALESCE(MAX(id),0) FROM messages
        """)
        conn.commit()



def _invalid(reason):
    return AdvertioError(reason, retryable=True)


def _country_options(schema, field):
    if not isinstance(schema, list):
        raise _invalid("cargo_country_schema_invalid")
    matches = [row for row in schema if isinstance(row, dict) and row.get("key") == field]
    if len(matches) != 1:
        raise _invalid("cargo_country_schema_missing")
    raw = matches[0].get("optionsJson", matches[0].get("options"))
    try:
        options = json.loads(raw) if isinstance(raw, str) else raw
        codes = [item["value"] for item in options]
        if not codes or not all(re.fullmatch(r"[A-Z]{2}", code) for code in codes):
            raise ValueError
        if len(codes) != len(set(codes)):
            raise ValueError
    except (TypeError, ValueError, KeyError):
        raise _invalid("cargo_country_schema_invalid") from None
    return sorted(codes)


def _fields(row, required):
    return isinstance(row, dict) and all(
        isinstance(row.get(key), str) and row[key].strip() for key in required
    ) and (row.get("nameFa") is None or isinstance(row["nameFa"], str))


def _download(client, previous):
    schema = client.catalog("categories/passenger-cargo/attributes")
    options = {side: _country_options(schema, side + "_country")
               for side in ("origin", "destination")}
    reference = client.catalog("locations/countries")
    if not isinstance(reference, list) or not reference or len(reference) >= CITY_LIMIT or any(
        not _fields(row, ("code", "name")) or not re.fullmatch(r"[A-Z]{2}", row["code"])
        for row in reference
    ):
        raise _invalid("cargo_country_catalog_incomplete")
    reference_by_code = {row["code"]: row for row in reference}
    allowed = set(options["origin"]) | set(options["destination"])
    if (len(reference_by_code) != len(reference) or not allowed <= reference_by_code.keys()
            or (previous and not set(previous.data["cities"]) <= allowed)):
        raise _invalid("cargo_country_catalog_incomplete")
    cities = {}
    for code in sorted(allowed):
        rows = client.list_cities(code)
        if not isinstance(rows, list) or len(rows) >= CITY_LIMIT or any(
            not _fields(row, ("code", "name", "provinceCode"))
            or row.get("countryCode", code) != code for row in rows
        ):
            raise _invalid("cargo_city_catalog_incomplete:" + code)
        keys = {(row["provinceCode"], row["code"]) for row in rows}
        if len(keys) != len(rows):
            raise _invalid("cargo_city_catalog_duplicate:" + code)
        # A sudden regression in a still-supported country's result can be a
        # partial upstream response. Preserve the last complete view.
        if previous and len(rows) < len(previous.data["cities"].get(code, ())):
            raise _invalid("cargo_city_catalog_regressed:" + code)
        cities[code] = rows
    if not any(cities.values()):
        raise _invalid("cargo_city_catalog_empty")
    return {"options": options, "countries": reference_by_code, "cities": cities}


class CargoCatalog:
    def __init__(self, data, synced_at):
        self.data = data
        self.synced_at = synced_at
        self.countries = defaultdict(set)
        self.cities = defaultdict(set)
        self.city_countries = defaultdict(set)
        self.country_evidence = defaultdict(set)
        for code, row in data["countries"].items():
            for value in (code, row["name"], row.get("nameFa")):
                if value:
                    self.countries[normalize(value)].add(code)
                    self.country_evidence[code].add(normalize(value))
        for alias, code in TRANSFER_COUNTRY_ALIASES:
            self.countries[normalize(alias)].add(code)
            self.country_evidence[code].add(normalize(alias))
        self.country_evidence["CA"].update(("ontario", "انتاریو"))
        self.country_evidence["GB"].update(("england", "انگلیس", "بریتانیا"))
        for country, rows in data["cities"].items():
            for row in rows:
                city = (country, row["code"], row["name"], row["provinceCode"])
                for value in (row["code"], row["name"], row.get("nameFa")):
                    if value:
                        self.cities[country, normalize(value)].add(city)
        for country, alias, name, _ in TRANSFER_CITY_ALIASES:
            matches = self.cities.get((country, normalize(name)), ())
            if matches:
                self.cities[country, normalize(alias)].update(matches)
        for (country, alias), matches in self.cities.items():
            if matches:
                self.city_countries[alias].add(country)

    @property
    def stale(self):
        return now() >= datetime.fromisoformat(self.synced_at) + TTL

    def resolve(self, direction, country, value, source_text):
        city_name = normalize(value)
        codes = self.countries.get(normalize(country), set())
        if not city_name:
            return None, "missing_city", ""
        canonical = next(iter(codes)) if len(codes) == 1 else ""
        if not canonical and len(self.city_countries.get(city_name, ())) > 1:
            return None, "ambiguous_city", ""
        if canonical not in self.data["options"][direction]:
            return None, "unsupported_country", canonical
        matches = self.cities.get((canonical, city_name), ())
        if len(matches) > 1:
            return None, "ambiguous_city", canonical
        if not matches:
            return None, "unsupported_city", canonical
        colliding = self.city_countries.get(city_name, set())
        if {"CA", "GB"} <= colliding:
            # Match a country cue attached to the city, even when a different
            # country appears elsewhere in the route.
            evidence = normalize(source_text)
            names = {alias for (code, alias), entries in self.cities.items()
                     if code == canonical and next(iter(matches)) in entries}
            city_pattern = "|".join(re.escape(name) for name in sorted(names) if name)
            country_pattern = "|".join(re.escape(name) for name in sorted(self.country_evidence[canonical])
                                       if len(name) > 2 or name in {"uk", "ca", "gb"})
            separator = r"[\s,،()/-]{1,5}"
            if not country_pattern or not re.search(
                rf"(?<!\w)(?:(?:{city_pattern}){separator}(?:{country_pattern})"
                rf"|(?:{country_pattern}){separator}(?:{city_pattern}))(?!\w)", evidence
            ):
                return None, "ambiguous_city", canonical
        return next(iter(matches)), None, canonical

    def register_sql(self, conn):
        def route_ok(origin_country, origin_city, dest_country, dest_city, text):
            return int(all(self.resolve(side, country, city, text)[0] is not None
                           for side, country, city in (
                               ("origin", origin_country, origin_city),
                               ("destination", dest_country, dest_city))))
        conn.create_function("advertio_cargo_route_ok", 5, route_ok, deterministic=True)


def _stored(conn, host):
    if not conn.execute("""
        SELECT 1 FROM sqlite_master WHERE type='table' AND name='advertio_cargo_catalog'
    """).fetchone():
        return None
    return conn.execute("SELECT * FROM advertio_cargo_catalog WHERE host=?", (host,)).fetchone()


def _snapshot(row):
    if row and row["payload"]:
        return CargoCatalog(json.loads(row["payload"]), row["synced_at"])
    return None


def load(conn, host=None):
    return _snapshot(_stored(conn, host or config.ADVERTIO_BASE_URL))


def register_sql(conn, snapshot):
    row = conn.execute("SELECT max_message_id FROM advertio_cargo_history_boundary WHERE id=1").fetchone()
    watermark = row[0] if row else 0

    def historical_scope(local_id, origin, destination):
        return int(local_id <= watermark and (
            str(origin or "").strip().upper() not in PREVIOUS_COUNTRIES
            or str(destination or "").strip().upper() not in PREVIOUS_COUNTRIES
        ))

    conn.create_function("advertio_cargo_history_scope", 3, historical_scope, deterministic=True)
    if snapshot is None:
        conn.create_function("advertio_cargo_route_ok", 5, lambda *_: 0, deterministic=True)
    else:
        snapshot.register_sql(conn)


def metadata(conn, host=None):
    row = _stored(conn, host or config.ADVERTIO_BASE_URL)
    if not row:
        return {"status": "missing"}
    payload = row["payload"]
    counts = {code: len(cities) for code, cities in json.loads(payload)["cities"].items()} if payload else {}
    return {
        "status": ("stale" if now() >= datetime.fromisoformat(row["synced_at"]) + TTL else "ready")
        if payload else "missing",
        "country_count": len(counts),
        "city_count": sum(counts.values()),
        "per_country": counts,
        "version": hashlib.sha256(payload.encode()).hexdigest()[:16] if payload else None,
        "last_success": row["synced_at"],
        "next_refresh": row["next_refresh"],
        "last_error": row["error"],
    }


def refresh(client, host):
    """Coalesce refreshes across processes. Never hold a DB lock during HTTP."""
    stamp = now()
    with closing(get_connection()) as conn:
        row = _stored(conn, host)
        if row and row["next_refresh"] > stamp.isoformat():
            return _snapshot(row)
        token = uuid4().hex
        timeout = getattr(client, "timeout", config.ADVERTIO_TIMEOUT_SECONDS)
        lease = (stamp + timedelta(seconds=(CITY_LIMIT + 2) * (2 * timeout + 1))).isoformat()
        cursor = conn.execute("""
            INSERT INTO advertio_cargo_catalog(host,next_refresh,owner) VALUES(?,?,?)
            ON CONFLICT(host) DO UPDATE SET next_refresh=excluded.next_refresh,owner=excluded.owner
            WHERE advertio_cargo_catalog.next_refresh<=?
        """, (host, lease, token, stamp.isoformat()))
        conn.commit()
        if not cursor.rowcount:
            return load(conn, host)
        previous = _snapshot(row)
    try:
        payload = json.dumps(_download(client, previous), sort_keys=True, ensure_ascii=False)
    except AdvertioError as exc:
        wait = max(RETRY.total_seconds(), exc.retry_after or 0)
        with closing(get_connection()) as conn, conn:
            conn.execute("""
                UPDATE advertio_cargo_catalog SET next_refresh=?,owner=NULL,error=?
                WHERE host=? AND owner=?
            """, ((now() + timedelta(seconds=wait)).isoformat(), str(exc), host, token))
        return previous
    completed = now()
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            UPDATE advertio_cargo_catalog SET payload=?,synced_at=?,next_refresh=?,owner=NULL,error=NULL
            WHERE host=? AND owner=?
        """, (payload, completed.isoformat(), (completed + TTL).isoformat(), host, token))
        return load(conn, host)
