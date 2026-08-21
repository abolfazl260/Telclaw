"""Advertio delivery orchestration for crawled housing listings."""

import json
import re
from datetime import datetime, timezone

import config
from delivery.advertio_client import AdvertioClient, AdvertioError
from storage.message_repository import MessageRepository


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
        candidates = [data.get("contact"), record.get("sender_username")]
        for value in candidates:
            text = str(value or "").strip()
            if re.fullmatch(r"@?[A-Za-z0-9_]{5,64}", text):
                return text if text.startswith("@") else f"@{text}"
        return None

    @staticmethod
    def _media_paths(record):
        path = record.get("media_path")
        if not path:
            return []
        return [path]

    @staticmethod
    def _date(value):
        if not value:
            return None
        text = str(value).strip()[:10]
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
        return text

    @classmethod
    def _infer_rental_duration(cls, data, record):
        """Resolve Advertio rental_duration from extracted data and, when absent, source text.

        Explicit AI extraction wins. Otherwise obvious duration phrases are mapped to the
        Advertio enum. Ambiguous/no duration defaults to long_term because this is the safest
        interpretation for ordinary monthly housing rentals and does not invent a short stay.
        """
        explicit = str(
            data.get("rental_duration")
            or data.get("rent_period")
            or ""
        ).strip().lower().replace("-", "_").replace(" ", "_")
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
        furnishing_aliases = {True: "furnished", False: "unfurnished", "true": "furnished", "false": "unfurnished"}
        furnishing = furnishing_aliases.get(furnishing, furnishing)
        if furnishing in {"furnished", "unfurnished", "partially"}:
            attributes["furnishing"] = furnishing

        rental_duration = cls._infer_rental_duration(data, record or {})
        if rental_duration in cls.RENTAL_DURATIONS:
            attributes["rental_duration"] = rental_duration

        area = cls._number(data.get("area"))
        if area is not None and 5 <= area <= 500:
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

    def get_pending_count(self, channel_username=None):
        return len(self.repository.get_advertio_pending(limit=1000000, channel_username=channel_username))

    def deliver_pending(self, limit=100, channel_username=None, progress=True):
        """Send already processed housing records without crawling or re-running AI."""
        records = self.repository.get_advertio_pending(limit=limit, channel_username=channel_username)
        total = len(records)
        sent = already_existed = failed = 0
        for index, record in enumerate(records, start=1):
            try:
                result = self.deliver(record, record["housing_data"])
                status = "already_existed" if result.get("already_existed") else "sent"
                self.repository.mark_advertio_result(
                    record["message_id"], record["channel_username"], status=status,
                    lead_id=result.get("lead_id"), error=None,
                    processed_at=datetime.now(timezone.utc).isoformat(),
                )
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
