"""Advertio delivery orchestration for crawled housing listings."""

import json
import re
from datetime import datetime, timezone

import config
from delivery.advertio_client import AdvertioClient, AdvertioError


class AdvertioMappingError(ValueError):
    pass


class AdvertioDeliveryService:
    """Maps Telclaw housing output to Advertio without coupling AI to the API."""

    PROPERTY_TYPES = {"apartment", "condo", "basement", "studio", "room", "house"}
    LISTING_TYPES = {"rent", "roommate"}
    BEDROOMS = {"0", "1", "2", "3", "4+"}

    def __init__(self, client=None):
        if client is None:
            if not config.ADVERTIO_INGEST_KEY:
                raise AdvertioMappingError("TELCLAW_ADVERTIO_INGEST_KEY is required when Advertio ingestion is enabled")
            client = AdvertioClient(
                config.ADVERTIO_BASE_URL,
                config.ADVERTIO_INGEST_KEY,
                timeout=config.ADVERTIO_TIMEOUT_SECONDS,
            )
        self.client = client
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

    @staticmethod
    def _canonical_property_type(value):
        value = str(value or "").strip().lower()
        aliases = {
            "flat": "apartment", "apt": "apartment", "apartment": "apartment",
            "condo": "condo", "condominium": "condo", "basement": "basement",
            "studio": "studio", "room": "room", "room in apartment": "room",
            "house": "house", "home": "house",
        }
        return aliases.get(value)

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

        optional_map = {
            "furnishing": data.get("furnished"),
            "rental_duration": data.get("rent_period"),
            "area": self._number(data.get("area")),
            "bathrooms_count": self._number(data.get("bathrooms")),
            "available_from": data.get("availability"),
        }
        furnishing_aliases = {True: "furnished", False: "unfurnished", "true": "furnished", "false": "unfurnished"}
        if optional_map["furnishing"] in furnishing_aliases:
            optional_map["furnishing"] = furnishing_aliases[optional_map["furnishing"]]
        if optional_map["furnishing"] in {"furnished", "unfurnished", "partially"}:
            attributes["furnishing"] = optional_map["furnishing"]
        if optional_map["rental_duration"] in {"daily", "short_term", "long_term"}:
            attributes["rental_duration"] = optional_map["rental_duration"]
        if optional_map["area"] is not None and 5 <= optional_map["area"] <= 500:
            attributes["area"] = optional_map["area"]
        if optional_map["bathrooms_count"] is not None and 1 <= optional_map["bathrooms_count"] <= 10:
            attributes["bathrooms_count"] = optional_map["bathrooms_count"]
        if optional_map["available_from"]:
            attributes["available_from"] = str(optional_map["available_from"])[:10]

        features = data.get("features")
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except json.JSONDecodeError:
                features = [features]
        if isinstance(features, list):
            amenities = [str(x).strip().lower() for x in features if str(x).strip()]
            if amenities:
                attributes["amenities"] = amenities

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
        """Upload media first, then create the lead. 400 is permanent; 5xx is retryable."""
        payload = self.build_payload(record, data)
        for path in self._media_paths(record)[:10]:
            key = self.client.upload_media(path, self.source_name)
            payload["mediaKeys"].append(key)

        result = self.client.create_lead(payload)
        return result

    def delete_original_post_listing(self, external_id):
        """Deactivate an Advertio listing after the original Telegram post is gone."""
        return self.client.delete_lead(self.source_name, str(external_id))

    def deactivate_source(self):
        return self.client.deactivate_source(self.source_name)
