"""Independent Advertio passenger-cargo ingest with restart-safe historical backfill.

Telegram publication and the existing housing Advertio state are never modified.
"""
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import config
from delivery.advertio_client import AdvertioClient, AdvertioError
from storage.database import get_connection

log = logging.getLogger("telclaw.advertio.cargo")
COUNTRIES = {"IR", "CA", "US", "DE", "IT", "GB", "FR"}
ITEMS = {"documents", "clothes", "electronics", "food", "other"}
CURRENCIES = {"CAD", "USD", "EUR", "GBP"}


def cargo_queue_eligibility_sql():
    """SQL for records that may be sent, shared by worker and dashboard.

    Requires aliases m (messages) and t (transferlist). Parameter order:
    (undated_message_cutoff, earliest_departure_date). The platform's city
    catalog is authoritative: this preliminary filter never guesses a city.
    """
    countries = ",".join("'" + country + "'" for country in sorted(COUNTRIES))
    return f"""
        t.origin_city IS NOT NULL AND TRIM(t.origin_city)<>''
        AND t.destination_city IS NOT NULL AND TRIM(t.destination_city)<>''
        AND UPPER(TRIM(COALESCE(t.origin_country,''))) IN ({countries})
        AND UPPER(TRIM(COALESCE(t.destination_country,''))) IN ({countries})
        AND (
            ((t.departure_date IS NULL OR TRIM(t.departure_date)='')
             AND substr(m.date,1,10)>=?)
            OR
            (t.departure_date IS NOT NULL AND TRIM(t.departure_date)<>''
             AND date(substr(t.departure_date,1,10))>=date(?))
        )
    """


class CargoMappingError(ValueError):
    """A source post cannot be represented faithfully by the cargo API."""


def source_name(record):
    # Telegram message IDs are unique within a channel, not globally.
    # Numeric channel ID survives a public username change.
    identity = record.get("channel_id")
    if identity is None:
        identity = str(record["channel_username"]).lower().lstrip("@")
    digest = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:16]
    return "tg-cargo-" + digest


def _name(value):
    value = str(value or "").strip().casefold()
    return value.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ").replace("_", " ").replace("-", " ").replace("  ", " ")


def _date(value):
    value = str(value or "").strip()[:10]
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CargoMappingError("Invalid Gregorian departure_date")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if 0 < v < float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _contact(record):
    # Do not assign channel ownership to a publisher account.
    explicit = str(record.get("contact") or "")
    match = re.search(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{5,32})\b", explicit)
    if match and match.group(1).casefold() != str(record.get("channel_username") or "").lstrip("@").casefold():
        return "@" + match.group(1)
    if record.get("sender_type") in {"user", "admin_forward_text"}:
        # Text contact from an administrator forward is unverified; never add a poster ID.
        username = str(record.get("sender_username") or "").lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return "@" + username
    return None


def _cargo_types(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else value
        except ValueError:
            pass
    parts = value if isinstance(value, list) else re.split(r"[,،;/+]|\band\b", str(value or ""), flags=re.I)
    aliases = {
        "document": "documents", "documents": "documents", "مدارک": "documents", "اسناد": "documents",
        "clothing": "clothes", "clothes": "clothes", "لباس": "clothes", "پوشاک": "clothes",
        "electronic": "electronics", "electronics": "electronics", "گوشی": "electronics",
        "موبایل": "electronics", "لپ تاپ": "electronics", "لپتاپ": "electronics",
        "food": "food", "خوراکی": "food", "مواد غذایی": "food",
        "other": "other", "سایر": "other",
    }
    result = []
    for part in parts:
        v = aliases.get(_name(part))
        if v and v not in result:
            result.append(v)
    return result


def _explicit_currency(text, currency):
    currency = str(currency or "").strip().upper()
    if currency not in CURRENCIES:
        return False
    patterns = {
        "CAD": r"\bCAD\b|\bC\$|Canadian dollars?|دلار\s*کانادا",
        "USD": r"\bUSD\b|\bUS\$|US dollars?|دلار\s*(?:آمریکا|امریکا)",
        "EUR": r"\bEUR\b|€|\beuros?\b|یورو",
        "GBP": r"\bGBP\b|£|\bpounds?\s*sterling\b|پوند\s*(?:انگلیس|بریتانیا)",
    }
    return bool(re.search(patterns[currency], text, re.I))


def _per_kg(text):
    return bool(re.search(r"\bper\s*(?:kg|kilo(?:gram)?)\b|/\s*kg\b|\bkg\s*rate\b|کیلویی|کیلوئی|هر\s*کیلو", text, re.I))


class AdvertioCargoService:
    def __init__(self, client=None):
        if client is None:
            if not config.ADVERTIO_INGEST_KEY:
                raise ValueError("TELCLAW_ADVERTIO_INGEST_KEY is required for cargo ingestion")
            client = AdvertioClient(
                config.ADVERTIO_BASE_URL, config.ADVERTIO_INGEST_KEY,
                timeout=config.ADVERTIO_TIMEOUT_SECONDS,
            )
        self.client = client
        self._cities = {}
        self._cities_checked_at = {}
        self._ensure_tables()
        self._recover_stale_sends()

    @staticmethod
    def _ensure_tables():
        conn = get_connection()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS advertio_cargo_deliveries (
                    processed_message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    lead_id TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_advertio_cargo_status
                    ON advertio_cargo_deliveries(status,next_attempt_at);
                CREATE TABLE IF NOT EXISTS advertio_cargo_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS advertio_cargo_attempts (
                    id INTEGER PRIMARY KEY,
                    processed_message_id INTEGER NOT NULL,
                    attempted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_advertio_cargo_attempts_date
                    ON advertio_cargo_attempts(attempted_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def _recover_stale_sends(self):
        # One delivery can fetch two city catalogs and then POST the lead.
        # Keep the historical 30-minute floor, allowing larger client timeouts.
        timeout = max(config.ADVERTIO_TIMEOUT_SECONDS,
                      getattr(self.client, "timeout", config.ADVERTIO_TIMEOUT_SECONDS))
        cutoff = (datetime.now(timezone.utc) - timedelta(
            seconds=max(1800, 3 * timeout + 120))).isoformat()
        conn = get_connection()
        try:
            # Avoid acquiring a write lock on healthy cycles. The UPDATE still
            # checks status/time atomically against a concurrent fresh claim.
            if not conn.execute("""SELECT 1 FROM advertio_cargo_deliveries
                    WHERE status='sending' AND updated_at<? LIMIT 1""", (cutoff,)).fetchone():
                return 0
            cursor = conn.execute("""UPDATE advertio_cargo_deliveries
                SET status='retry',next_attempt_at=NULL
                WHERE status='sending' AND updated_at<?""", (cutoff,))
            conn.commit()
            if cursor.rowcount:
                log.info("Recovered %s stale cargo sending records", cursor.rowcount)
            return cursor.rowcount
        finally:
            conn.close()

    def pending(self, limit=50, channel_username=None, before_datetime=None, row_ids=None):
        # Backfill is automatic: all historically processed transferlist rows are
        # eligible unless delivered/rejected, expired, or older than the freshness
        # window without a future travel date.
        today = datetime.now(timezone.utc)
        cutoff = (today - timedelta(days=config.ADVERTIO_CARGO_BACKFILL_DAYS)).date().isoformat()
        conn = get_connection()
        try:
            sql = """
                SELECT m.id AS message_row_id, m.message_id, m.channel_id,
                       m.channel_username, m.message_link, m.sender_id,
                       m.sender_username, m.sender_type, m.raw_text,
                       m.cleaned_text, m.date AS message_date,
                       t.title, t.description, t.origin_city, t.origin_country,
                       t.destination_city, t.destination_country, t.departure_date,
                       t.cargo_type, t.weight, t.weight_unit, t.price,
                       t.currency, t.contact, COALESCE(d.attempts,0) AS attempts
                  FROM messages m
                  JOIN transferlist t ON t.processed_message_id=m.id
                  LEFT JOIN advertio_cargo_deliveries d ON d.processed_message_id=m.id
                 WHERE m.processing_status='processed' AND m.ai_status='processed'
                   AND m.ai_category='transferlist'
                   AND COALESCE(d.status,'waiting') IN ('waiting','retry')
                   AND (d.next_attempt_at IS NULL OR d.next_attempt_at<=?)
                   AND (""" + cargo_queue_eligibility_sql() + """)
            """
            values = [today.isoformat(), cutoff, today.date().isoformat()]
            if before_datetime:
                sql += " AND (d.updated_at IS NULL OR d.updated_at<?)"
                values.append(before_datetime)
            if channel_username:
                sql += " AND m.channel_username=?"
                values.append(channel_username)
            if row_ids is not None:
                # Operator selections are constrained by the same live
                # eligibility/status/retry-window predicate as normal sends.
                ids = tuple(dict.fromkeys(int(item) for item in row_ids))
                if not ids or len(ids) > 100 or any(item <= 0 for item in ids):
                    raise ValueError("Choose 1 to 100 valid cargo record IDs")
                sql += " AND m.id IN (" + ",".join("?" for _ in ids) + ")"
                values.extend(ids)
            # Time-sensitive trips should be delivered first; undated ads
            # follow newest-first. Do not let re-crawled expired trips or
            # unsupported routes consume an entire small manual batch.
            sql += """
                ORDER BY
                  CASE WHEN t.departure_date IS NOT NULL
                             AND TRIM(t.departure_date)<>'' THEN 0 ELSE 1 END,
                  CASE WHEN t.departure_date IS NOT NULL
                             AND TRIM(t.departure_date)<>''
                       THEN date(substr(t.departure_date,1,10)) END ASC,
                  CASE WHEN t.departure_date IS NULL
                             OR TRIM(t.departure_date)=''
                       THEN m.date END DESC,
                  m.id DESC
                LIMIT ?
            """
            values.append(int(limit))
            return [dict(row) for row in conn.execute(sql, values).fetchall()]
        finally:
            conn.close()

    def _claim(self, message_row_id):
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO advertio_cargo_deliveries
                    (processed_message_id,status,updated_at)
                VALUES (?,'waiting',?)
            """, (message_row_id, now))
            cursor = conn.execute("""
                UPDATE advertio_cargo_deliveries SET status='sending',updated_at=?
                 WHERE processed_message_id=? AND status IN ('waiting','retry')
                   AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            """, (now, message_row_id, now))
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def _result(self, record, status, *, lead_id=None, error=None, retry_after=None):
        now = datetime.now(timezone.utc)
        attempts = int(record.get("attempts") or 0) + 1
        next_time = None
        if status == "retry":
            delay = retry_after if retry_after else min(21600, 60 * (2 ** min(attempts, 8)))
            next_time = (now + timedelta(seconds=delay)).isoformat()
        conn = get_connection()
        try:
            conn.execute("""
                UPDATE advertio_cargo_deliveries
                   SET status=?, lead_id=?, error=?, attempts=?, next_attempt_at=?, updated_at=?
                 WHERE processed_message_id=?
            """, (status, lead_id, str(error)[:4000] if error else None,
                  attempts, next_time, now.isoformat(), record["message_row_id"]))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _pause_until():
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM advertio_cargo_meta WHERE key='paused_until'"
            ).fetchone()
            if row and row[0] > datetime.now(timezone.utc).isoformat():
                return row[0]
            return None
        finally:
            conn.close()

    @staticmethod
    def _pause(seconds):
        until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        conn = get_connection()
        try:
            conn.execute("""
                INSERT INTO advertio_cargo_meta(key,value) VALUES('paused_until',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (until,))
            conn.commit()
        finally:
            conn.close()
        return until

    @staticmethod
    def _record_api_attempt(message_row_id):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO advertio_cargo_attempts(processed_message_id,attempted_at) VALUES(?,?)",
                (message_row_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _remaining_budget():
        conn = get_connection()
        try:
            day = datetime.now(timezone.utc).date().isoformat()
            count = conn.execute(
                "SELECT COUNT(*) FROM advertio_cargo_attempts WHERE attempted_at>=?", (day,)
            ).fetchone()[0]
            return max(0, config.ADVERTIO_CARGO_DAILY_LIMIT - count)
        finally:
            conn.close()

    def _city(self, country, value):
        country = str(country or "").strip().upper()
        if country not in COUNTRIES:
            raise CargoMappingError("Cargo route country outside Advertio catalog: " + country)
        if country not in self._cities or time.monotonic() - self._cities_checked_at.get(country, 0) >= 86400:
            # One refresh per country per day, even with 5-minute crawl cycles.
            # Only the public API decides canonical names and province codes.
            entries = self.client.list_cities(country)
            if not isinstance(entries, list):
                raise AdvertioError("Unexpected city catalog response", retryable=True)
            if not entries:
                raise AdvertioError("Empty Advertio location catalog for " + country, retryable=True)
            lookup = {}
            for city in entries:
                for alias in (city.get("code"), city.get("name"), city.get("nameFa")):
                    if alias:
                        lookup[_name(alias)] = city
            self._cities[country] = lookup
            self._cities_checked_at[country] = time.monotonic()
        city = self._cities[country].get(_name(value))
        if not city:
            raise CargoMappingError("City is not in Advertio catalog: " + country + "/" + str(value))
        return city

    def build_payload(self, record):
        from_country = str(record.get("origin_country") or "").strip().upper()
        to_country = str(record.get("destination_country") or "").strip().upper()
        if not record.get("origin_city") or not record.get("destination_city"):
            raise CargoMappingError("Cargo route requires origin and destination cities")
        source = self._city(from_country, record["origin_city"])
        destination = self._city(to_country, record["destination_city"])
        departure = _date(record.get("departure_date"))
        if departure is not None and departure < datetime.now(timezone.utc).date():
            raise CargoMappingError("Cargo trip date has already passed")
        description = str(record.get("description") or record.get("cleaned_text") or "").strip()
        if not description:
            raise CargoMappingError("Cargo description is empty")
        attributes = {
            "origin_country": from_country,
            "origin": source["name"],
            "destination_country": to_country,
            "destination": destination["name"],
        }
        if departure is not None:
            attributes["travel_date"] = departure.isoformat()
        types = _cargo_types(record.get("cargo_type"))
        if types:
            attributes["cargo_type"] = types
        weight = _number(record.get("weight"))
        unit = _name(record.get("weight_unit"))
        if weight is not None:
            if unit in {"kg", "kilogram", "kilograms", "کیلو", "کیلوگرم"}:
                pass
            elif unit in {"g", "gram", "grams", "گرم"}:
                weight /= 1000
            elif unit in {"lb", "lbs", "pound", "pounds"}:
                weight *= 0.45359237
            else:
                weight = None
        if weight is not None and 0.1 <= weight <= 30:
            attributes["weight_kg"] = round(weight, 3)
        source_text = str(record.get("raw_text") or description)
        price = _number(record.get("price"))
        currency = str(record.get("currency") or "").strip().upper()
        if price is not None and _per_kg(source_text) and _explicit_currency(source_text, currency):
            attributes["price"] = price
            attributes["currency"] = currency
        elif price is None and re.search(r"توافقی|قیمت\s*توافقی|negotiable|price\s+on\s+request", source_text, re.I):
            attributes["price_negotiable"] = True
        contact = _contact(record)
        url = str(record.get("message_link") or "").strip()[:500] or None
        if not contact and not url:
            raise CargoMappingError("No verified Telegram contact handle or source URL")
        title = source["name"] + " → " + destination["name"]
        if departure:
            title += " · " + str(departure.day) + " " + departure.strftime("%b")
        payload = {
            "sourceName": source_name(record),
            "externalId": str(record["message_id"]),
            "sourceUrl": url,
            "contactHandle": contact,
            "title": title[:200],
            "description": description[:2000],
            "categorySlug": "passenger-cargo",
            "attributesJson": json.dumps(attributes, ensure_ascii=False, separators=(",", ":")),
            "countryCode": from_country,
            "province": source["provinceCode"],
            "city": source["code"],
            "mediaKeys": [],
            # Every newly introduced journey begins under human moderation.
            "autoPublish": bool(config.ADVERTIO_CARGO_AUTO_PUBLISH),
        }
        author_id = record.get("sender_id")
        if record.get("sender_type") == "user" and author_id and int(author_id) > 0:
            payload["posterTelegramUserId"] = int(author_id)
        return payload

    def deliver_pending(self, limit=None, channel_username=None, before_datetime=None,
                        should_stop=None, progress=False, row_ids=None):
        self._recover_stale_sends()
        limit = min(int(limit or config.ADVERTIO_CARGO_BATCH_SIZE), self._remaining_budget())
        result = {"found": 0, "sent": 0, "already_existed": 0,
                  "rejected": 0, "retry": 0, "budget_remaining": self._remaining_budget()}
        paused_until = self._pause_until()
        if paused_until:
            result["paused_until"] = paused_until
            return result
        if limit <= 0:
            return result
        records = self.pending(limit=limit, channel_username=channel_username,
                               before_datetime=before_datetime, row_ids=row_ids)
        result["found"] = len(records)
        for record in records:
            if should_stop and should_stop():
                result["stopped"] = True
                break
            if not self._claim(record["message_row_id"]):
                continue
            try:
                payload = self.build_payload(record)
                self._record_api_attempt(record["message_row_id"])
                response = self.client.create_lead(payload)
                status = "already_existed" if response.get("already_existed") else "sent"
                self._result(record, status, lead_id=response["lead_id"])
                result[status] += 1
            except CargoMappingError as exc:
                self._result(record, "rejected", error=str(exc))
                result["rejected"] += 1
                log.warning("Cargo mapping rejected message=%s channel=%s: %s",
                            record["message_id"], record["channel_username"], exc)
            except AdvertioError as exc:
                retry = exc.retryable or exc.status in (401, 404)
                status = "retry" if retry else "rejected"
                self._result(record, status, error=str(exc),
                             retry_after=21600 if exc.status in (401, 404, 429) else None)
                result[status] += 1
                log.warning("Cargo ingest %s message=%s http=%s: %s",
                            status, record["message_id"], exc.status, exc)
                if retry:
                    # Stop all cargo requests until the destination recovers; a
                    # per-row retry timer alone would hammer other waiting rows.
                    seconds = 21600 if exc.status in (401, 404, 429) else 600
                    result["paused_until"] = self._pause(seconds)
                    break
            except Exception as exc:
                self._result(record, "retry", error=str(exc))
                result["retry"] += 1
                result["paused_until"] = self._pause(600)
                log.exception("Unexpected cargo delivery failure for %s", record["message_id"])
                break
            if progress:
                log.info("Cargo ingest progress: %s/%s", sum(result[k] for k in
                         ("sent", "already_existed", "rejected", "retry")), len(records))
        result["budget_remaining"] = self._remaining_budget()
        return result
