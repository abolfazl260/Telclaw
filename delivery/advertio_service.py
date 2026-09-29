"""Advertio delivery orchestration for crawled housing listings."""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import config
from delivery.advertio_client import AdvertioClient, AdvertioError
from storage.data_normalizer import (
    availability_rent_period,
    extract_telegram_handle,
    normalize_housing_area,
    normalize_housing_availability,
    normalize_housing_contact,
)
from storage.message_repository import MessageRepository

logger = logging.getLogger(__name__)


class AdvertioMappingError(ValueError):
    pass


class AdvertioDeliveryService:
    """Maps Telclaw housing output to Advertio without coupling AI to the API."""

    PROPERTY_TYPES = {"apartment", "condo", "basement", "studio", "room", "house"}
    LISTING_TYPES = {"rent", "roommate"}
    BEDROOMS = {"0", "1", "2", "3", "4+"}
    RENTAL_DURATIONS = {"daily", "short_term", "long_term"}
    AMENITIES = {
        "elevator", "parking", "storage", "balcony", "terrace", "garden", "rooftop",
        "security_system", "cctv", "doorman", "renovated", "kitchen_appliances",
        "washing_machine", "dishwasher", "air_conditioning", "heating", "internet_ready",
        "pool", "sauna", "gym",
    }
    LIFESTYLE_TAGS = {
        "quiet", "early_bird", "night_owl", "social", "party_friendly", "private",
        "student_only", "professional", "remote_worker", "vegetarian",
    }

    def __init__(self, client=None, repository=None):
        if client is None:
            if not config.ADVERTIO_INGEST_KEY:
                raise AdvertioMappingError("TELCLAW_ADVERTIO_INGEST_KEY is required when Advertio ingestion is enabled")
            client = AdvertioClient(
                config.ADVERTIO_BASE_URL,
                config.ADVERTIO_INGEST_KEY,
                timeout=config.ADVERTIO_TIMEOUT_SECONDS,
            )
        self.client = client
        self.repository = repository or MessageRepository()
        self.source_name = config.ADVERTIO_SOURCE_NAME

    @staticmethod
    def _text(value, max_length=None):
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:max_length] if max_length else text

    @staticmethod
    def _number(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _canonical_property_type(cls, value):
        """Normalize AI property_type; Advertio defaults unknown/empty values to house."""
        value = str(value or "").strip().lower()
        aliases = {
            "flat": "apartment", "apt": "apartment", "apartment": "apartment",
            "condo": "condo", "condominium": "condo", "basement": "basement",
            "studio": "studio", "room": "room", "room in apartment": "room",
            "house": "house", "home": "house",
        }
        return aliases.get(value, "house")

    @staticmethod
    def _canonical_listing_type(value):
        value = str(value or "").strip().lower()
        aliases = {"rent": "rent", "rental": "rent", "for rent": "rent", "roommate": "roommate", "shared": "roommate", "room share": "roommate"}
        return aliases.get(value)

    @classmethod
    def _canonical_bedrooms(cls, value):
        if value is None:
            return None
        text = str(value).strip().lower().replace(" bedrooms", "").replace(" bedroom", "").strip()
        if text in cls.BEDROOMS:
            return text
        try:
            number = float(text)
            if number.is_integer() and int(number) in range(0, 4):
                return str(int(number))
            if number >= 4:
                return "4+"
        except ValueError:
            pass
        return None

    @staticmethod
    def _contact_handle(data, record):
        normalized_contact = normalize_housing_contact(data.get("contact"))
        handle = extract_telegram_handle(normalized_contact, allow_plain=False)
        if handle:
            return handle
        # sender_username comes directly from Telegram metadata, so a bare value
        # is safe to interpret as a Telegram username.
        return extract_telegram_handle(record.get("sender_username"), allow_plain=True)

    @staticmethod
    def _media_paths(record):
        value = record.get("media_paths")
        paths = []
        if isinstance(value, (list, tuple)):
            paths = [str(path) for path in value if path]
        elif isinstance(value, str) and value.strip():
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                decoded = []
            if isinstance(decoded, list):
                paths = [str(path) for path in decoded if path]

        if not paths and record.get("media_path"):
            paths = [str(record["media_path"])]

        ordered = []
        seen = set()
        for path in paths:
            if path not in seen:
                ordered.append(path)
                seen.add(path)
        return ordered[:10]

    def _cleanup_delivered_media(self, record):
        """Remove all delivered local media after a successful Advertio delivery.

        Cleanup is deliberately best-effort: a cleanup failure must never turn a
        successfully recorded Advertio delivery into a retry/failure. The database
        paths are cleared only when every local file was removed or already absent.
        """
        paths = self._media_paths(record)
        if not paths:
            return

        cleanup_failed = False
        for path_value in paths:
            path = Path(path_value)
            try:
                path.unlink()
            except FileNotFoundError:
                logger.warning("Advertio media cleanup skipped; file already missing: %s", path)
            except OSError as exc:
                cleanup_failed = True
                logger.error("Advertio media cleanup failed for %s: %s", path, exc)

        if cleanup_failed:
            return

        try:
            self.repository.clear_media_path(
                record["message_id"],
                record["channel_username"],
            )
            record["media_path"] = None
            record["media_paths"] = []
        except Exception as exc:
            logger.error(
                "Advertio media_path DB cleanup failed for message=%s channel=%s: %s",
                record.get("message_id"), record.get("channel_username"), exc,
            )

    @staticmethod
    def _date(value):
        return normalize_housing_availability(value)

    @classmethod
    def _infer_rental_duration(cls, data, record):
        """Resolve Advertio rental_duration from extracted data and, when absent, source text.

        Explicit AI extraction wins. Otherwise obvious duration phrases are mapped to the
        Advertio enum. Ambiguous/no duration defaults to long_term because this is the safest
        interpretation for ordinary monthly housing rentals and does not invent a short stay.
        """
        explicit_source = (
            data.get("rental_duration")
            or data.get("rent_period")
            or availability_rent_period(data.get("availability"))
            or ""
        )
        explicit = str(explicit_source).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "daily": "daily", "day": "daily", "per_day": "daily", "daily_rental": "daily",
            "short_term": "short_term", "shortterm": "short_term", "short": "short_term",
            "weekly": "short_term", "week": "short_term", "per_week": "short_term",
            "monthly": "long_term", "month": "long_term", "per_month": "long_term",
            "long_term": "long_term", "longterm": "long_term", "long": "long_term",
        }
        if explicit in aliases:
            return aliases[explicit]

        text_parts = [
            data.get("description"), data.get("title"), data.get("raw_text"),
            data.get("text"), record.get("raw_text"), record.get("text"),
        ]
        text = " ".join(str(value or "") for value in text_parts).lower()
        if not text:
            return "long_term"

        daily_patterns = (
            r"\b(?:daily|per\s*day|day[- ]to[- ]day|nightly|per\s*night|nightly\s*rent)\b",
            r"\b(?:روزانه|شبی|هر\s*روز|هر\s*شب)\b",
        )
        short_patterns = (
            r"\b(?:short[- ]?term|weekly|per\s*week|week[- ]to[- ]week|vacation|temporary|monthly\s*stay)\b",
            r"\b(?:کوتاه\s*مدت|هفتگی|موقت|تعطیلات)\b",
        )
        if any(re.search(pattern, text) for pattern in daily_patterns):
            return "daily"
        if any(re.search(pattern, text) for pattern in short_patterns):
            return "short_term"
        return "long_term"

    @classmethod
    def _optional_attributes(cls, data, listing_type, record=None):
        attributes = {}

        furnishing = data.get("furnished")
        furnishing_aliases = {
            True: "furnished", False: "unfurnished",
            "1": "furnished", "yes": "furnished", "true": "furnished",
            "0": "unfurnished", "no": "unfurnished", "false": "unfurnished",
            "partial": "partially", "partially furnished": "partially",
        }
        if isinstance(furnishing, str):
            furnishing = furnishing.strip().lower()
        furnishing = furnishing_aliases.get(furnishing, furnishing)
        if furnishing in {"furnished", "unfurnished", "partially"}:
            attributes["furnishing"] = furnishing

        rental_duration = cls._infer_rental_duration(data, record or {})
        if rental_duration in cls.RENTAL_DURATIONS:
            attributes["rental_duration"] = rental_duration

        area, area_unit = normalize_housing_area(data.get("area"), data.get("area_unit"))
        area = cls._number(area)
        if area_unit == "sqm" and area is not None and 5 <= area <= 500:
            attributes["area"] = area

        bathrooms = cls._number(data.get("bathrooms"))
        if bathrooms is not None and 1 <= bathrooms <= 10 and (bathrooms * 2) % 1 == 0:
            attributes["bathrooms_count"] = bathrooms

        floor = cls._number(data.get("floor_number", data.get("floor")))
        if floor is not None and 0 <= floor <= 100:
            attributes["floor_number"] = floor

        year_built = str(data.get("year_built") or "").strip().lower()
        if year_built in {"0_5", "5_10", "10_20", "20_plus"}:
            attributes["year_built"] = year_built

        for source_key in ("pets_allowed", "smoking_allowed", "is_owner"):
            value = data.get(source_key)
            if isinstance(value, bool):
                attributes[source_key] = value
            elif isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                attributes[source_key] = value.strip().lower() == "true"

        available_from = cls._date(data.get("available_from", data.get("availability")))
        if available_from:
            attributes["available_from"] = available_from

        features = data.get("features", data.get("amenities"))
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except json.JSONDecodeError:
                features = [features]
        if isinstance(features, list):
            amenities = [str(x).strip().lower().replace(" ", "_") for x in features if str(x).strip()]
            amenities = [x for x in amenities if x in cls.AMENITIES]
            if amenities:
                attributes["amenities"] = sorted(set(amenities))

        if listing_type == "roommate":
            gender = str(data.get("gender_preference") or "").strip().lower()
            if gender in {"male", "female", "family", "any"}:
                attributes["gender_preference"] = gender

            age_range = data.get("age_range")
            if isinstance(age_range, (list, tuple)) and len(age_range) == 2:
                start, end = cls._number(age_range[0]), cls._number(age_range[1])
                if start is not None and end is not None and 18 <= start <= end <= 70:
                    attributes["age_range"] = [start, end]

            lifestyle = data.get("lifestyle_tags")
            if isinstance(lifestyle, str):
                try:
                    lifestyle = json.loads(lifestyle)
                except json.JSONDecodeError:
                    lifestyle = [lifestyle]
            if isinstance(lifestyle, list):
                tags = [str(x).strip().lower().replace(" ", "_") for x in lifestyle if str(x).strip()]
                tags = [x for x in tags if x in cls.LIFESTYLE_TAGS]
                if tags:
                    attributes["lifestyle_tags"] = sorted(set(tags))

        return attributes

    def build_payload(self, record, data):
        if not isinstance(data, dict):
            raise AdvertioMappingError("Housing AI data must be an object")

        listing_type = self._canonical_listing_type(data.get("listing_type"))
        property_type = self._canonical_property_type(data.get("property_type"))
        bedrooms = self._canonical_bedrooms(data.get("bedrooms"))
        price = self._number(data.get("price"))
        currency = str(data.get("currency") or "").strip().upper()
        country_code = str(data.get("country_code") or "").strip().upper()
        province = self._text(data.get("province"))
        city = self._text(data.get("city"))

        missing = []
        if listing_type not in self.LISTING_TYPES:
            missing.append("listing_type")
        if property_type not in self.PROPERTY_TYPES:
            missing.append("property_type")
        if bedrooms not in self.BEDROOMS:
            missing.append("bedrooms")
        if price is None or not 100 <= price <= 10000:
            missing.append("price")
        if currency != "CAD":
            missing.append("currency=CAD")
        if country_code != "CA":
            missing.append("country_code=CA")
        if not province:
            missing.append("province")
        if not city:
            missing.append("city")
        if missing:
            raise AdvertioMappingError("Required Advertio housing data is missing/invalid: " + ", ".join(missing))

        title = self._text(data.get("title"), 200)
        description = self._text(data.get("description"), 2000)
        source_url = self._text(record.get("message_link"), 500)
        contact_handle = self._contact_handle(data, record)
        if not source_url and not contact_handle:
            raise AdvertioMappingError("Advertio requires sourceUrl or contactHandle")
        if not title:
            raise AdvertioMappingError("Advertio title is required")

        attributes = {
            "listing_type": listing_type,
            "property_type": property_type,
            "bedrooms": bedrooms,
            "price": price,
        }
        attributes.update(self._optional_attributes(data, listing_type, record))

        return {
            "sourceName": self.source_name,
            "externalId": str(record["message_id"]),
            "sourceUrl": source_url,
            "contactHandle": contact_handle,
            "title": title,
            "description": description,
            "categorySlug": "housing",
            "attributesJson": json.dumps(attributes, ensure_ascii=False, separators=(",", ":")),
            "countryCode": "CA",
            "province": province,
            "city": city,
            "neighborhood": self._text(data.get("neighborhood"), 100),
            "mediaKeys": [],
            "autoPublish": bool(config.ADVERTIO_AUTO_PUBLISH),
        }

    def deliver(self, record, data):
        """Upload media first, then create the lead. 400 is permanent; 429/5xx are retryable."""
        payload = self.build_payload(record, data)
        for path in self._media_paths(record)[:10]:
            key = self.client.upload_media(path, self.source_name)
            payload["mediaKeys"].append(key)
        return self.client.create_lead(payload)

    def finalize_successful_delivery(self, record, result, *, repository=None, processed_at=None):
        """Persist successful delivery state before releasing local media.

        Both new leads and Advertio's idempotent already-existed response are
        successful terminal outcomes. Local files are deleted only after the
        database status has been persisted successfully.
        """
        target_repository = repository or self.repository
        status = "already_existed" if result.get("already_existed") else "sent"
        target_repository.mark_advertio_result(
            record["message_id"],
            record["channel_username"],
            status=status,
            lead_id=result.get("lead_id"),
            error=None,
            processed_at=processed_at or datetime.now(timezone.utc).isoformat(),
        )
        self._cleanup_delivered_media(record)
        return status

    def prepare_media_for_delivery(self, record, media_downloader=None):
        """Guarantee photo listings have usable local media before Advertio delivery.

        Photo records are fail-closed: if their local media cache is missing or
        stale, a downloader must restore it. A missing downloader, download error,
        empty result, or missing downloaded file is retryable and must never fall
        through to a lead request with mediaKeys=[].
        """
        if record.get("media_type") != "photo":
            return self._media_paths(record)

        existing = self._media_paths(record)
        if existing and all(Path(path).is_file() for path in existing):
            record["media_paths"] = existing[:10]
            record["media_path"] = record["media_paths"][0]
            return record["media_paths"]

        if media_downloader is None:
            raise AdvertioError(
                "Telegram photo media is not available locally and no media downloader is configured",
                retryable=True,
            )

        try:
            downloaded = media_downloader(record)
        except AdvertioError:
            raise
        except Exception as exc:
            raise AdvertioError(
                f"Telegram media download failed: {exc}",
                retryable=True,
            ) from exc

        if isinstance(downloaded, str):
            downloaded = [downloaded]
        if not isinstance(downloaded, (list, tuple)):
            downloaded = []

        paths = [str(path) for path in downloaded if path][:10]
        if not paths:
            raise AdvertioError(
                "Telegram media download returned no usable photo",
                retryable=True,
            )

        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            raise AdvertioError(
                f"Telegram media download returned missing local file: {missing[0]}",
                retryable=True,
            )

        record["media_paths"] = paths
        record["media_path"] = paths[0]
        return paths

    def get_pending_count(self, channel_username=None):
        return len(self.repository.get_advertio_pending(limit=1000000, channel_username=channel_username))

    def deliver_pending(self, limit=100, channel_username=None, progress=True, media_downloader=None, before_datetime=None):
        """Send processed housing records, optionally limited to work predating a cycle cutoff."""
        if before_datetime is None:
            records = self.repository.get_advertio_pending(
                limit=limit,
                channel_username=channel_username,
            )
        else:
            records = self.repository.get_advertio_pending(
                limit=limit,
                channel_username=channel_username,
                before_datetime=before_datetime,
            )
        total = len(records)
        sent = already_existed = failed = 0
        for index, record in enumerate(records, start=1):
            try:
                self.prepare_media_for_delivery(record, media_downloader=media_downloader)
                result = self.deliver(record, record["housing_data"])
                status = self.finalize_successful_delivery(record, result)
                if status == "already_existed":
                    already_existed += 1
                else:
                    sent += 1
                if progress:
                    print(f"[ADVERTIO] {index}/{total} ({index * 100 / total:6.2f}%) {status}: message={record['message_id']}")
            except Exception as exc:
                retryable = isinstance(exc, AdvertioError) and exc.retryable
                status = "retry" if retryable else "rejected"
                self.repository.mark_advertio_result(
                    record["message_id"], record["channel_username"], status=status,
                    lead_id=getattr(exc, "lead_id", None), error=str(exc)[:4000],
                    processed_at=datetime.now(timezone.utc).isoformat(),
                )
                failed += 1
                if progress:
                    print(f"[ADVERTIO] {index}/{total} ({index * 100 / total:6.2f}%) {status}: message={record['message_id']} reason={str(exc)[:300]}")
        return {"found": total, "sent": sent, "already_existed": already_existed, "failed": failed}

    def delete_original_post_listing(self, external_id):
        return self.client.delete_lead(self.source_name, str(external_id))

    def deactivate_source(self):
        return self.client.deactivate_source(self.source_name)
