"""Independent Advertio passenger-cargo ingest with restart-safe historical backfill.

Telegram publication and the existing housing Advertio state are never modified.
"""
import hashlib
import json
import logging
import re
from random import uniform
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import config
from delivery import advertio_cargo_catalog as catalog
from delivery.advertio_client import AdvertioClient, AdvertioError
from delivery.advertio_lifecycle import create_listing
from delivery.advertio_source_text import description_limit, explicitly_negotiable, has_cargo_price, original_text
from storage.database import get_connection
from storage.cargo_types import normalize_cargo_types as _cargo_types
from delivery.cargo_publication import outcome, OUTCOMES

log = logging.getLogger("telclaw.advertio.cargo")
ITEMS = {"documents", "clothes", "electronics", "food", "other"}


def cargo_queue_eligibility_sql():
    """Shared route and date eligibility. Requires the local catalog SQL function."""
    return f"""
        advertio_cargo_route_ok(t.origin_country,t.origin_city,
            t.destination_country,t.destination_city,{catalog.ROUTE_EVIDENCE_SQL})=1
        AND {catalog.TRIP_WINDOW_SQL}
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
    sender_type = record.get("sender_type")
    if not (sender_type == "admin_forward_text"
            or (sender_type == "user" and record.get("sender_is_original") != 0)):
        return None
    username = str(record.get("sender_username") or "").lstrip("@")
    channel = str(record.get("channel_username") or "").lstrip("@").casefold()
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) and username.casefold() != channel:
        return "@" + username
    return None




class AdvertioCargoService:
    def __init__(self, client=None):
        if client is None:
            if not config.ADVERTIO_INGEST_KEY:
                raise ValueError("TELCLAW_ADVERTIO_INGEST_KEY is required for cargo ingestion")
            client = AdvertioClient(
                config.ADVERTIO_BASE_URL, config.ADVERTIO_INGEST_KEY,
                timeout=config.ADVERTIO_TIMEOUT_SECONDS, category="cargo",
            )
        self.client = client
        self.host = getattr(client, "base_url", config.ADVERTIO_BASE_URL)
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

    def pending(self, limit=50, channel_username=None, before_datetime=None, row_ids=None, *, snapshot=None):
        # Backfill is automatic: all historically processed transferlist rows are
        # eligible unless delivered/rejected, expired, or older than the freshness
        # window without a future travel date.
        today = datetime.now(timezone.utc)
        cutoff = (today - timedelta(days=config.ADVERTIO_CARGO_BACKFILL_DAYS)).date().isoformat()
        conn = get_connection()
        try:
            catalog.register_sql(conn, snapshot or catalog.load(conn, self.host))
            sql = """
                SELECT m.id AS message_row_id, m.message_id, m.channel_id,
                       m.channel_username, m.message_link, m.sender_id,
                       m.sender_username, m.sender_type, m.sender_is_original, m.raw_text,
                       m.text, m.cleaned_text, m.date AS message_date,
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

    def _result(self, record, status, *, lead_id=None, error=None, retry_after=None, attempted=True):
        now = datetime.now(timezone.utc)
        attempts = int(record.get("attempts") or 0) + int(attempted)
        next_time = None
        if status == "retry":
            delay = retry_after if retry_after else min(21600, 60 * (2 ** min(attempts, 8)) * uniform(0.9, 1.1))
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

    def build_payload(self, record, *, snapshot=None):
        if snapshot is None:
            snapshot = catalog.refresh(self.client, self.host)
        if snapshot is None:
            raise AdvertioError("cargo_catalog_unavailable", retryable=True)
        resolved = []
        for side in ("origin", "destination"):
            city, reason, country = snapshot.resolve(
                side, record.get(side + "_country"), record.get(side + "_city"),
                original_text(record),
            )
            if reason:
                if snapshot.stale:
                    raise AdvertioError("cargo_catalog_stale", retryable=True)
                raise CargoMappingError(f"{side}:{reason}:{country}")
            resolved.append(city)
        (from_country, source_code, source_name_en, source_province), (
            to_country, _, destination_name_en, _
        ) = resolved
        departure = _date(record.get("departure_date"))
        if departure is not None and departure < datetime.now(timezone.utc).date():
            raise CargoMappingError("Cargo trip date has already passed")
        description = original_text(record)
        if not description:
            raise CargoMappingError("Cargo description is empty")
        attributes = {
            "origin_country": from_country,
            "origin": source_name_en,
            "destination_country": to_country,
            "destination": destination_name_en,
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
        source_text = description
        price = _number(record.get("price"))
        currency = str(record.get("currency") or "").strip().upper()
        if price is not None and has_cargo_price(source_text, price, currency):
            attributes["price"] = price
            attributes["currency"] = currency
        elif explicitly_negotiable(source_text):
            attributes["price_negotiable"] = True
        contact = _contact(record)
        url = str(record.get("message_link") or "").strip()[:500] or None
        if not contact and not url:
            raise CargoMappingError("No verified Telegram contact handle or source URL")
        title = source_name_en + " → " + destination_name_en
        if departure:
            title += " · " + str(departure.day) + " " + departure.strftime("%b")
        source = source_name(record)
        payload = {
            "sourceName": source,
            "externalId": str(record["message_id"]),
            "sourceUrl": url,
            "contactHandle": contact,
            "title": title[:200],
            "description": description_limit(description),
            "categorySlug": "passenger-cargo",
            "attributesJson": json.dumps(attributes, ensure_ascii=False, separators=(",", ":")),
            "countryCode": from_country,
            "province": source_province,
            "city": source_code,
            "mediaKeys": [],
            # Advertio retains authority over duplicate detection and moderation.
            "autoPublish": bool(config.ADVERTIO_CARGO_AUTO_PUBLISH),
        }
        author_id = record.get("sender_id")
        if (record.get("sender_type") == "user" and record.get("sender_is_original") in (1, None)
                and str(author_id or "").isdigit() and 0 < int(author_id) < 2 ** 63):
            payload["posterTelegramUserId"] = int(author_id)
        return payload

    def deliver_pending(self, limit=None, channel_username=None, before_datetime=None,
                        should_stop=None, progress=False, row_ids=None):
        self._recover_stale_sends()
        limit = min(int(limit or config.ADVERTIO_CARGO_BATCH_SIZE), self._remaining_budget())
        result = {"found": 0, "sent": 0, "already_existed": 0,
                  "rejected": 0, "retry": 0, "budget_remaining": self._remaining_budget(),
                  **dict.fromkeys(OUTCOMES, 0)}
        paused_until = self._pause_until()
        if paused_until:
            result["paused_until"] = paused_until
            return result
        if limit <= 0:
            return result
        if should_stop and should_stop():
            return {**result, "stopped": True}
        snapshot = catalog.refresh(self.client, self.host)
        if snapshot is None:
            return {**result, "catalog_deferred": True}
        records = self.pending(limit=limit, channel_username=channel_username,
                               before_datetime=before_datetime, row_ids=row_ids, snapshot=snapshot)
        result["found"] = len(records)
        for record in records:
            if should_stop and should_stop():
                result["stopped"] = True
                break
            if not self._claim(record["message_row_id"]):
                continue
            try:
                payload = self.build_payload(record, snapshot=snapshot)
                response = create_listing(self.client, payload, record, "cargo")
                self._record_api_attempt(record["message_row_id"])
                status = "already_existed" if response.get("already_existed") else "sent"
                self._result(record, status, lead_id=response["lead_id"])
                result[status] += 1
                if status == "sent":
                    result[outcome(response.get("status"))] += 1
            except CargoMappingError as exc:
                self._result(record, "rejected", error=str(exc))
                result["rejected"] += 1
                log.warning("Cargo mapping rejected message=%s channel=%s: %s",
                            record["message_id"], record["channel_username"], exc)
            except AdvertioError as exc:
                if exc.deferred:
                    self._result(record, "retry", error=str(exc), retry_after=exc.retry_after,
                                 attempted=False)
                    result["deferred"] = result.get("deferred", 0) + 1
                    break
                self._record_api_attempt(record["message_row_id"])
                retry = exc.retryable or exc.status in (401, 404)
                status = "retry" if retry else "rejected"
                delay = exc.retry_after or (3600 if exc.status in (401, 404)
                                            else 60 if exc.status == 429 else None)
                self._result(record, status, error=str(exc), retry_after=delay)
                result[status] += 1
                log.warning("Cargo ingest %s message=%s http=%s: %s",
                            status, record["message_id"], exc.status, exc)
                if retry:
                    # Stop all cargo requests until the destination recovers; a
                    # per-row retry timer alone would hammer other waiting rows.
                    seconds = delay or 600
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
