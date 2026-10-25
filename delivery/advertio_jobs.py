"""Conservative Jobs mapping and an independent restart-safe Advertio outbox."""

import hashlib
import json
import logging
import math
import re
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

from delivery.jobs_discovery import discovery_report
import config
from delivery.advertio_client import AdvertioClient, AdvertioError
from storage.database import get_connection

log = logging.getLogger("telclaw.advertio.jobs")
CATEGORIES = {
    "restaurant_food": "Restaurant & Food",
    "construction": "Construction",
    "cleaning": "Cleaning",
    "delivery_driver": "Delivery & Driving",
    "retail": "Retail & Store",
    "beauty_personal_care": "Beauty & Salon",
    "office_admin": "Office & Admin",
    "it_tech": "IT & Tech",
    "general_labour": "General Labour",
    "warehouse": "Warehouse",
    "skilled_trades": "Skilled Trades",
    "customer_service": "Customer Service",
    "sales": "Sales",
    "healthcare_support": "Healthcare & Care",
    "childcare": "Childcare",
    "education": "Teaching & Tutoring",
    "design_creative": "Design & Creative",
    "other": "Other",
}
ENUMS = {
    "role": {"employer", "seeker"},
    "job_category": set(CATEGORIES),
    "job_type": {"full_time", "part_time", "contract", "temporary"},
    "work_arrangement": {"on_site", "hybrid", "remote"},
    "salary_period": {"hour", "month", "year"},
}
KEYS = set(ENUMS) | {"job_title", "salary"}
REQUIRED = {"role", "job_category", "job_type"}
TYPE_EVIDENCE = {
    "full_time": r"\bfull[ -]?time\b|تمام[‌\s-]*وقت",
    "part_time": r"\bpart[ -]?time\b|پاره[‌\s-]*وقت|نیمه[‌\s-]*وقت",
    "contract": r"\bcontract(?:or|ual)?\b|قراردادی|پیمانکاری",
    "temporary": r"\btemporary\b|\bseasonal\b|موقت|فصلی",
}
SCAM_SIGNALS = {
    "upfront_fee": r"(?:application|registration|recruitment|training|equipment)\s+fee|pay.{0,25}(?:to apply|before.{0,12}(?:start|hiring))|هزینه.{0,20}(?:ثبت[‌\s]*نام|استخدام)|پرداخت.{0,20}(?:بیعانه|ودیعه|پیش[‌\s]*پرداخت)",
    "bank_credentials": r"(?:bank(?:ing)?|online bank).{0,25}(?:password|login|credential|PIN)|(?:رمز|پسورد).{0,20}(?:بانک|حساب)|\b(?:OTP|one.time password)\b",
    "identity_harvesting": r"(?:send|upload|provide).{0,50}(?:passport|identity document|social insurance|\bSIN\b)|(?:ارسال|آپلود|عکس).{0,35}(?:پاسپورت|کارت ملی|مدرک هویتی)",
    "suspicious_payment": r"(?:deposit|send|transfer|pay).{0,40}(?:crypto|bitcoin|gift card)|(?:واریز|پرداخت|خرید).{0,25}(?:رمزارز|بیت[‌\s]*کوین|گیفت[‌\s]*کارت)|(?:receive|deposit).{0,25}(?:cheque|check).{0,40}(?:send|forward|transfer)",
}


def now():
    return datetime.now(timezone.utc)


def normalized(value):
    return " ".join(
        str(value or "")
        .strip()
        .casefold()
        .replace("ي", "ی")
        .replace("ك", "ک")
        .replace("\u200c", " ")
        .split()
    )


def source_name(record):
    identity = record.get("channel_id")
    if identity is None:
        identity = (
            str(record.get("channel_username") or "").strip().casefold().lstrip("@")
        )
    if not str(identity or "").strip():
        raise JobsMappingError("missing_source_identity")
    return "tg-jobs-" + hashlib.sha256(str(identity).encode()).hexdigest()[:16]


class JobsMappingError(ValueError):
    """Fixed operator-facing reason code; never include source text or credentials."""


def source_approved(record):
    approved = {
        normalized(v).lstrip("@")
        for v in config.ADVERTIO_JOBS_SOURCES.split(",")
        if v.strip()
    }
    return bool(
        {
            str(record.get("channel_id") or ""),
            normalized(record.get("channel_username")).lstrip("@"),
        }
        & approved
    )


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value


class JobsCatalog:
    """TTL cache of live schema and exact city search; never a truncated nationwide list."""

    def __init__(self, client):
        self.client = client
        self.cache = {}

    def get(self, path, **params):
        key = (path, tuple(sorted(params.items())))
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        body = self.client.catalog(path, **params)
        if not isinstance(body, list):
            raise AdvertioError("catalog_shape_changed", retryable=True)
        # Bound negative lookup cache on long-running workers.
        if len(self.cache) >= 1000:
            self.cache.clear()
        self.cache[key] = (time.monotonic(), body)
        return body

    def schema(self):
        rows = self.get("categories/jobs/attributes")
        attrs = {a.get("key"): a for a in rows if isinstance(a, dict)}
        if not REQUIRED <= set(attrs) or any(
            a.get("isRequired") and k not in KEYS for k, a in attrs.items()
        ):
            raise AdvertioError("jobs_schema_changed")
        for key in KEYS:
            expected = (
                "Number"
                if key == "salary"
                else ("Text" if key == "job_title" else "Select")
            )
            if attrs.get(key, {}).get("type") != expected:
                raise AdvertioError("jobs_schema_changed")
        for key, values in ENUMS.items():
            a = attrs.get(key)
            options = _object(a.get("optionsJson", a.get("options"))) if a else None
            if (
                not isinstance(options, list)
                or {o.get("value") for o in options} != values
            ):
                raise AdvertioError("jobs_schema_changed")
        return attrs

    def city(self, country, province, value):
        if str(country or "").upper() != "CA" or not value:
            raise JobsMappingError("missing_canadian_city")
        countries = self.get("locations/countries")
        if not any(c.get("code") == "CA" for c in countries):
            raise AdvertioError("canada_catalog_unavailable", retryable=True)
        provinces = self.get("locations/provinces", countryCode="CA", limit=200)
        match = [
            p
            for p in provinces
            if normalized(province)
            in {normalized(p.get(k)) for k in ("code", "name", "nameFa")}
            and province
        ]
        if province and len(match) != 1:
            raise JobsMappingError("unknown_province")
        # Search both English and Persian source values. q is substring matching:
        # only EXACT canonical code/name/nameFa matches below are accepted.
        query = dict(countryCode="CA", q=str(value).strip(), limit=200)
        if match:
            query["provinceCode"] = match[0]["code"]
        entries = self.get("locations/cities", **query)
        if len(entries) >= 200:
            # No pagination contract exists. Refuse ambiguous/truncated searches.
            raise JobsMappingError("city_search_truncated")
        cities = {
            (c.get("provinceCode"), c.get("code")): c
            for c in entries
            if normalized(value)
            in {normalized(c.get(k)) for k in ("code", "name", "nameFa")}
            and c.get("code")
            and c.get("name")
            and c.get("provinceCode")
            and c.get("countryCode", "CA") == "CA"
            and (not match or c.get("provinceCode") == match[0]["code"])
        }
        if len(cities) != 1:
            raise JobsMappingError("unresolved_or_ambiguous_city")
        city = next(iter(cities.values()))
        if not any(p.get("code") == city["provinceCode"] for p in provinces):
            raise JobsMappingError("unknown_province")
        return city


def salary_attributes(record, text, schema):
    """Trust AI extraction; enforce only the receiving API's numeric constraints.

    CAD is the authorized default. An explicit foreign currency is never relabelled.
    Salary period is optional in the active backend schema; omit it when unknown.
    """
    currency = str(record.get("salary_currency") or "CAD").strip().upper()
    if currency != "CAD":
        return {}
    try:
        amount = float(record.get("salary"))
    except (TypeError, ValueError, OverflowError):
        return {}
    bounds = _object(schema.get("salary", {}).get("validationJson")) or {}
    if (
        isinstance(record.get("salary"), bool)
        or not math.isfinite(amount)
        or not max(1, bounds.get("min", 1))
        <= amount
        <= min(1000000, bounds.get("max", 1000000))
    ):
        return {}
    attributes = {"salary": amount}
    period = record.get("salary_period")
    if isinstance(period, str) and period in ENUMS["salary_period"]:
        attributes["salary_period"] = period
    return attributes


def compensation_description(description, record):
    """Make undisclosed pay visible with the current API; preserve original content.

    Jobs currently has no salary_negotiable attribute. A display-only annotation
    avoids sending an unsupported key or manufacturing a numeric salary.
    """
    if record.get("salary") not in (None, ""):
        return description
    if re.search(r"negotiable|توافقی|قابل\s*مذاکره", description, re.I):
        return description
    return description + "\n\nحقوق: توافقی (Negotiable)"


class AdvertioJobsService:
    def __init__(self, client=None):
        self.client = client or AdvertioClient(
            config.ADVERTIO_BASE_URL,
            config.ADVERTIO_INGEST_KEY,
            config.ADVERTIO_TIMEOUT_SECONDS,
            category="jobs",
        )
        self.catalog = JobsCatalog(self.client)

    def build_payload(self, record):
        if (
            record.get("ai_status") != "processed"
            or record.get("ai_category") != "joblist"
            or record.get("processing_status") != "processed"
            or record.get("classification_category") != "joblist"
            or record.get("classification_status") != "processed"
        ):
            raise JobsMappingError("ai_not_processed_joblist")
        if record.get("is_job_listing") not in (True, 1, "true"):
            raise JobsMappingError("not_verified_employment_listing")
        if not source_approved(record) or record.get("sender_type") == "bot":
            raise JobsMappingError("source_not_approved")
        description = record.get("cleaned_text")
        if not isinstance(description, str) or len(description.strip()) < 20:
            raise JobsMappingError("insufficient_cleaned_description")
        # Hold rather than truncate facts. UTF-16 limit matches the receiving .NET API.
        if len(description.encode("utf-16-le")) // 2 > 2000:
            raise JobsMappingError("description_exceeds_2000_no_truncation")
        attrs = {}
        for key in REQUIRED:
            value = record.get(key)
            if not isinstance(value, str) or value not in ENUMS[key]:
                raise JobsMappingError("invalid_or_missing_" + key)
            attrs[key] = value
        if not re.search(TYPE_EVIDENCE[attrs["job_type"]], description, re.I):
            raise JobsMappingError("employment_type_without_source_evidence")
        if attrs["job_category"] == "other" and not record.get("job_title"):
            raise JobsMappingError("other_requires_identified_occupation")
        schema = self.catalog.schema()
        city = self.catalog.city(
            record.get("country_code"), record.get("province"), record.get("city")
        )
        # Require the canonical city (or its catalog alias) to appear in original text.
        aliases = [city.get(k) for k in ("name", "nameFa", "code") if city.get(k)]
        if not any(
            re.search(
                r"(?<!\w)" + re.escape(normalized(a)) + r"(?!\w)",
                normalized(description),
            )
            for a in aliases
        ):
            raise JobsMappingError("city_without_source_evidence")
        job_title = record.get("job_title")
        if (
            isinstance(job_title, str)
            and job_title.strip()
            and len(job_title.strip()) <= 60
        ):
            attrs["job_title"] = job_title.strip()
        arrangement = record.get("work_arrangement")
        if isinstance(arrangement, str) and arrangement in ENUMS["work_arrangement"]:
            attrs["work_arrangement"] = arrangement
        attrs.update(salary_attributes(record, description, schema))
        description = compensation_description(description, record)
        if len(description.encode("utf-16-le")) // 2 > 2000:
            raise JobsMappingError("description_exceeds_2000_with_compensation_label")
        for key, value in attrs.items():
            if key not in schema:
                raise AdvertioError("jobs_schema_changed")
        field_options = _object(
            schema["job_category"].get(
                "optionsJson", schema["job_category"].get("options")
            )
        )
        label = next(
            o.get("label") for o in field_options if o["value"] == attrs["job_category"]
        )
        title = (
            ("Hiring" if attrs["role"] == "employer" else "Looking for work")
            + " · "
            + (attrs.get("job_title") or label)
            + " · "
            + city["name"]
        )
        if len(title) > 200:
            raise JobsMappingError("title_exceeds_200")
        mid = str(record.get("external_id") or record.get("message_id") or "")
        if not re.fullmatch(r"[1-9]\d*", mid):
            raise JobsMappingError("invalid_original_message_id")
        url = str(record.get("message_link") or "").strip()
        username_from_source = (
            str(record.get("channel_username") or "").strip().lstrip("@")
        )
        if not url and re.fullmatch(r"[A-Za-z0-9_]{4,32}", username_from_source):
            # Some legacy text-only collector rows have no message_link. The
            # source username and original message ID still identify the post.
            url = f"https://t.me/{username_from_source}/{mid}"
        # Only a source post, not an arbitrary URL or unrelated contact from AI.
        url_ok = re.fullmatch(
            r"https://t\.me/(?:[A-Za-z0-9_]{4,32}|c/[1-9]\d*)/" + re.escape(mid), url
        )
        if url_ok:
            channel = url.split("/")[3].casefold()
            if channel == "c":
                expected = str(record.get("channel_id") or "").removeprefix("-100")
                if url.split("/")[4] != expected:
                    url_ok = False
            elif (
                channel
                != str(record.get("channel_username") or "").lstrip("@").casefold()
            ):
                url_ok = False
        contact = None
        author_id = record.get("sender_id")
        original = (
            record.get("sender_is_original") in (1, True)
            and record.get("sender_type") == "user"
        )
        username = str(record.get("sender_username") or "").lstrip("@")
        if (
            original
            and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            and username.casefold()
            != str(record.get("channel_username") or "").lstrip("@").casefold()
        ):
            contact = "@" + username
        if not url_ok and not contact:
            raise JobsMappingError("missing_verified_contact_or_source_url")
        flags = sorted(
            key
            for key, pattern in SCAM_SIGNALS.items()
            if re.search(pattern, description, re.I | re.S)
        )
        payload = dict(
            sourceName=record.get("source_name") or source_name(record),
            externalId=mid,
            sourceUrl=url if url_ok else None,
            contactHandle=contact,
            title=title,
            description=description,
            categorySlug="jobs",
            attributesJson=json.dumps(attrs, ensure_ascii=False, separators=(",", ":")),
            countryCode="CA",
            province=city["provinceCode"],
            city=city["code"],
            mediaKeys=[],
            autoPublish=bool(config.ADVERTIO_JOBS_AUTO_PUBLISH and not flags),
        )
        if (
            original
            and isinstance(author_id, int)
            and not isinstance(author_id, bool)
            and author_id > 0
        ):
            payload["posterTelegramUserId"] = author_id
        return payload, flags

    @staticmethod
    def _meta(conn, key, default=""):
        row = conn.execute(
            "SELECT value FROM advertio_jobs_meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set_meta(conn, key, value):
        conn.execute(
            "INSERT INTO advertio_jobs_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def enqueue(self, limit=100, *, backfill=False, row_ids=None):
        """No historical discovery unless an operator explicitly calls bounded backfill."""
        if backfill and not config.ADVERTIO_JOBS_BACKFILL_ENABLED:
            raise ValueError("Jobs backfill is disabled")
        days = (
            min(config.ADVERTIO_JOBS_FRESHNESS_DAYS, config.ADVERTIO_JOBS_BACKFILL_DAYS)
            if backfill
            else config.ADVERTIO_JOBS_FRESHNESS_DAYS
        )
        with closing(get_connection()) as conn:
            admission = discovery_report(conn, backfill=backfill, at=now())
            enqueued = 0
            watermark = int(self._meta(conn, "deployment_watermark", "0"))
            sql = """SELECT m.* FROM messages m JOIN joblist j ON j.processed_message_id=m.id
                LEFT JOIN advertio_jobs_deliveries d ON d.processed_message_id=m.id
                WHERE d.processed_message_id IS NULL AND m.processing_status='processed'
                AND m.ai_status='processed' AND m.ai_category='joblist'
                AND julianday(m.date)>=julianday(?) AND julianday(m.date)<=julianday(?)"""
            values = [(now() - timedelta(days=days)).isoformat(), now().isoformat()]
            if not backfill:
                sql += " AND m.id>?"
                values.append(watermark)
            if row_ids is not None:
                ids = tuple(int(v) for v in row_ids)
                if not ids or len(ids) > 100 or any(v < 1 for v in ids):
                    raise ValueError("Select 1 to 100 Jobs records")
                sql += " AND m.id IN (" + ",".join("?" for _ in ids) + ")"
                values.extend(ids)
            sql += " ORDER BY m.id DESC LIMIT ?"
            values.append(min(100, max(1, int(limit))))
            for row in conn.execute(sql, values).fetchall():
                record = dict(row)
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO advertio_jobs_deliveries
                    (processed_message_id,source_name,external_id,updated_at) VALUES(?,?,?,?)""",
                    (
                        row["id"],
                        source_name(record),
                        str(row["message_id"]),
                        now().isoformat(),
                    ),
                )
                enqueued += cursor.rowcount
            conn.commit()
            return {**admission, "enqueued": enqueued}

    def pending(self, limit, before_datetime=None):
        with closing(get_connection()) as conn:
            return [
                dict(r)
                for r in conn.execute(
                    """SELECT j.*,m.id AS message_row_id,
                m.message_id,m.channel_id,m.channel_username,m.message_link,m.cleaned_text,m.raw_text,
                m.sender_id,m.sender_username,m.sender_type,m.sender_is_original,m.ai_status,m.ai_category,
                m.processing_status,m.classification_category,m.classification_status,
                m.date AS message_date,d.source_name,d.external_id,d.attempts
                FROM advertio_jobs_deliveries d JOIN messages m ON m.id=d.processed_message_id
                JOIN joblist j ON j.processed_message_id=m.id
                WHERE d.status IN ('waiting','retry') AND (d.next_attempt_at IS NULL OR d.next_attempt_at<=?)
                AND (d.status='waiting' OR d.updated_at<?) ORDER BY m.id DESC LIMIT ?""",
                    (
                        now().isoformat(),
                        before_datetime or now().isoformat(),
                        int(limit),
                    ),
                )
            ]

    def _acquire(self, token):
        with closing(get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = now()
            if self._meta(conn, "worker_until") > current.isoformat():
                return False
            self._set_meta(conn, "worker", token)
            self._set_meta(
                conn,
                "worker_until",
                (current + timedelta(seconds=self._lease_seconds())).isoformat(),
            )
            self._set_meta(conn, "stop", "0")
            cutoff = (current - timedelta(seconds=self._lease_seconds())).isoformat()
            conn.execute(
                "UPDATE advertio_jobs_deliveries SET status='retry',next_attempt_at=NULL,updated_at=?,worker_token=NULL WHERE status='sending' AND updated_at<?",
                (current.isoformat(), cutoff),
            )
            conn.execute(
                "UPDATE advertio_jobs_attempts SET status='abandoned',finished_at=?,error_code='worker_interrupted' WHERE status='sending' AND attempted_at<?",
                (current.isoformat(), cutoff),
            )
            conn.commit()
            return True

    @staticmethod
    def _lease_seconds():
        return max(
            config.ADVERTIO_JOBS_CLAIM_TIMEOUT_SECONDS,
            int(config.ADVERTIO_TIMEOUT_SECONDS * 12) + 120,
        )

    def _claim(self, record, token):
        with closing(get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._meta(conn, "worker") != token:
                return False
            self._set_meta(
                conn,
                "worker_until",
                (now() + timedelta(seconds=self._lease_seconds())).isoformat(),
            )
            cursor = conn.execute(
                "UPDATE advertio_jobs_deliveries SET status='sending',attempts=attempts+1,updated_at=?,worker_token=? WHERE processed_message_id=? AND status IN ('waiting','retry') AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                (now().isoformat(), token, record["message_row_id"], now().isoformat()),
            )
            conn.commit()
            return cursor.rowcount == 1

    def _finish(
        self,
        record,
        token,
        status,
        *,
        response=None,
        error=None,
        delay=None,
        flags=None,
        attempt_id=None,
        http=None,
    ):
        response = response or {}
        stamp = now().isoformat()
        next_at = (
            (now() + timedelta(seconds=delay)).isoformat()
            if delay is not None
            else None
        )
        with closing(get_connection()) as conn:
            conn.execute(
                """UPDATE advertio_jobs_deliveries SET status=?,lead_id=?,remote_status=?,error_code=?,
                next_attempt_at=?,updated_at=?,safety_flags=?,worker_token=NULL
                WHERE processed_message_id=? AND status='sending' AND worker_token=?""",
                (
                    status,
                    response.get("lead_id"),
                    response.get("status"),
                    error,
                    next_at,
                    stamp,
                    json.dumps(flags or []),
                    record["message_row_id"],
                    token,
                ),
            )
            if attempt_id:
                conn.execute(
                    """UPDATE advertio_jobs_attempts SET status=?,finished_at=?,http_status=?,lead_id=?,remote_status=?,error_code=? WHERE id=?""",
                    (
                        status,
                        stamp,
                        http,
                        response.get("lead_id"),
                        response.get("status"),
                        error,
                        attempt_id,
                    ),
                )
            conn.commit()

    def _attempt(self, record):
        with closing(get_connection()) as conn:
            stamp = now().isoformat()
            conn.execute(
                "UPDATE advertio_jobs_deliveries SET first_attempt_at=COALESCE(first_attempt_at,?),last_attempt_at=? WHERE processed_message_id=?",
                (stamp, stamp, record["message_row_id"]),
            )
            cursor = conn.execute(
                "INSERT INTO advertio_jobs_attempts(processed_message_id,attempted_at) VALUES(?,?)",
                (record["message_row_id"], stamp),
            )
            conn.commit()
            return cursor.lastrowid

    def _pause(self, reason, seconds):
        with closing(get_connection()) as conn:
            self._set_meta(conn, "pause_reason", reason)
            self._set_meta(
                conn, "paused_until", (now() + timedelta(seconds=seconds)).isoformat()
            )
            conn.commit()

    def deliver_pending(
        self, limit=None, before_datetime=None, should_stop=None, progress=False
    ):
        result = {
            k: 0
            for k in (
                "found",
                "sent",
                "already_existed",
                "blocked",
                "rejected",
                "retry",
            )
        }
        if (
            not config.ADVERTIO_INGEST_ENABLED
            or not config.ADVERTIO_JOBS_ENABLED
            or not config.ADVERTIO_INGEST_KEY
        ):
            return {**result, "disabled": True}
        token = uuid.uuid4().hex
        if not self._acquire(token):
            return {**result, "busy": True}
        try:
            with closing(get_connection()) as conn:
                epoch = self._meta(conn, "catalog_epoch")
                if epoch != getattr(self, "_catalog_epoch", None):
                    self.catalog.cache.clear()
                    self._catalog_epoch = epoch
                pause = self._meta(conn, "paused_until")
                reason = self._meta(conn, "pause_reason")
                if (
                    reason in ("http_401", "http_404", "jobs_schema_changed")
                    or pause > now().isoformat()
                ):
                    return {**result, "paused_until": pause, "pause_reason": reason}
            if self.remaining_budget() <= 0:
                return {**result, "budget_exhausted": True}
            size = max(
                1,
                min(
                    int(limit or config.ADVERTIO_JOBS_BATCH_SIZE),
                    config.ADVERTIO_JOBS_BATCH_SIZE,
                    100,
                ),
            )
            result["discovery"] = self.enqueue(size)
            for record in self.pending(size, before_datetime):
                with closing(get_connection()) as conn:
                    stop = self._meta(conn, "stop") == "1"
                if (
                    stop
                    or (should_stop and should_stop())
                    or not config.ADVERTIO_JOBS_ENABLED
                    or not config.ADVERTIO_INGEST_ENABLED
                ):
                    result["stopped"] = True
                    break
                if self.remaining_budget() <= 0:
                    result["budget_exhausted"] = True
                    break
                if not self._claim(record, token):
                    continue
                result["found"] += 1
                attempt_id = None
                flags = []
                try:
                    if record["attempts"] >= config.ADVERTIO_JOBS_MAX_ATTEMPTS:
                        raise JobsMappingError("retry_limit_exhausted")
                    try:
                        posted = datetime.fromisoformat(
                            record["message_date"].replace("Z", "+00:00")
                        )
                        if posted.tzinfo is None:
                            posted = posted.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        raise JobsMappingError("invalid_source_date")
                    if now() - posted > timedelta(
                        days=config.ADVERTIO_JOBS_FRESHNESS_DAYS
                    ):
                        raise JobsMappingError("stale_source_post")
                    if posted > now() + timedelta(minutes=5):
                        raise JobsMappingError("future_source_date")
                    payload, flags = self.build_payload(record)
                    with closing(get_connection()) as conn:
                        stopping = self._meta(conn, "stop") == "1"
                    if (
                        stopping
                        or not config.ADVERTIO_JOBS_ENABLED
                        or not config.ADVERTIO_INGEST_ENABLED
                        or (should_stop and should_stop())
                    ):
                        self._finish(record, token, "waiting")
                        result["stopped"] = True
                        break
                    with closing(get_connection()) as conn:
                        conn.execute(
                            "UPDATE advertio_jobs_deliveries SET validated_at=?,safety_flags=? WHERE processed_message_id=?",
                            (
                                now().isoformat(),
                                json.dumps(flags),
                                record["message_row_id"],
                            ),
                        )
                        conn.commit()
                    attempt_id = self._attempt(record)
                    response = self.client.create_lead(payload)
                    status = (
                        "already_existed" if response.get("already_existed") else "sent"
                    )
                    self._finish(
                        record,
                        token,
                        status,
                        response=response,
                        flags=flags,
                        attempt_id=attempt_id,
                        http=response.get("http_status"),
                    )
                    result[status] += 1
                    log.info(
                        "Jobs delivery id=%s lead=%s outcome=%s remote=%s",
                        record["message_row_id"],
                        response.get("lead_id"),
                        status,
                        response.get("status"),
                    )
                except JobsMappingError as exc:
                    self._finish(record, token, "blocked", error=str(exc), flags=flags)
                    result["blocked"] += 1
                except AdvertioError as exc:
                    reason = (
                        "http_" + str(exc.status)
                        if exc.status
                        else (
                            "jobs_schema_changed"
                            if str(exc) == "jobs_schema_changed"
                            else "transport_or_catalog_error"
                        )
                    )
                    retry = exc.retryable or exc.status in (401, 404)
                    delay = max(
                        60, min(21600, 60 * 2 ** min(record["attempts"] + 1, 8))
                    )
                    if exc.retry_after is not None:
                        # Do not cap the provider's Retry-After to a shorter wait.
                        delay = max(delay, float(exc.retry_after))
                    status = "retry" if retry else "rejected"
                    if reason == "jobs_schema_changed":
                        status = "retry"
                    self._finish(
                        record,
                        token,
                        status,
                        error=reason,
                        delay=delay if status == "retry" else None,
                        flags=flags,
                        attempt_id=attempt_id,
                        http=exc.status,
                    )
                    result[status] += 1
                    log.warning(
                        "Jobs delivery id=%s outcome=%s reason=%s",
                        record["message_row_id"],
                        status,
                        reason,
                    )
                    if status == "retry":
                        self._pause(reason, delay)
                        if exc.status in (401, 404) or reason == "jobs_schema_changed":
                            from storage.database import record_system_activity

                            record_system_activity(
                                "advertio_jobs_fault",
                                level="ERROR",
                                source="advertio_jobs",
                                message=reason,
                            )
                        break
                except Exception:
                    self._finish(
                        record,
                        token,
                        "retry",
                        error="unexpected_delivery_error",
                        delay=600,
                        flags=flags,
                        attempt_id=attempt_id,
                    )
                    result["retry"] += 1
                    self._pause("unexpected_delivery_error", 600)
                    log.error(
                        "Jobs delivery id=%s failed unexpectedly",
                        record["message_row_id"],
                    )
                    break
            return result
        finally:
            with closing(get_connection()) as conn:
                if self._meta(conn, "worker") == token:
                    self._set_meta(conn, "worker_until", "")
                    self._set_meta(conn, "worker", "")
                    conn.commit()

    @staticmethod
    def remaining_budget():
        with closing(get_connection()) as conn:
            day = now().date().isoformat()
            posts = conn.execute(
                "SELECT COUNT(*) FROM advertio_jobs_attempts WHERE attempted_at>=?",
                (day,),
            ).fetchone()[0]
            jobs = conn.execute(
                "SELECT COUNT(*) FROM advertio_request_log WHERE attempted_at>=? AND category='jobs'",
                (day,),
            ).fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM advertio_request_log WHERE attempted_at>=?",
                (day,),
            ).fetchone()[0]
            return max(
                0,
                min(
                    config.ADVERTIO_JOBS_DAILY_LIMIT - max(posts, jobs),
                    config.ADVERTIO_DAILY_REQUEST_LIMIT - total,
                ),
            )

    @staticmethod
    def stop():
        with closing(get_connection()) as conn:
            AdvertioJobsService._set_meta(conn, "stop", "1")
            conn.commit()

    @staticmethod
    def resume():
        """Explicit operator acknowledgement after correcting authentication/deployment."""
        with closing(get_connection()) as conn:
            AdvertioJobsService._set_meta(conn, "catalog_epoch", now().isoformat())
            for key in ("pause_reason", "paused_until"):
                AdvertioJobsService._set_meta(conn, key, "")
            conn.commit()

    @staticmethod
    def requeue(message_row_id):
        with closing(get_connection()) as conn:
            cursor = conn.execute(
                "UPDATE advertio_jobs_deliveries SET status='waiting',attempts=0,error_code=NULL,next_attempt_at=NULL,updated_at=? WHERE processed_message_id=? AND status IN ('blocked','rejected')",
                (now().isoformat(), int(message_row_id)),
            )
            conn.commit()
            if cursor.rowcount != 1:
                raise ValueError("Only blocked/rejected Jobs can be requeued")
