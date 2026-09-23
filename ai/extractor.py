"""AI extraction service using Groq JSON mode with local validation."""

import json
import logging
import re

import requests

import config
from ai.category_schemas import CATEGORIES, validate_result
from ai.prompt_loader import render_prompt
from ai.rate_limiter import RateLimiter

logger = logging.getLogger("telclaw.ai")


class AIExtractionError(RuntimeError):
    """An AI failure whose details are diagnostic-only and must not reach message storage."""

    def __init__(self, message, *, status=None, reason=None, provider="groq", stop_queue=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.provider = provider
        self.stop_queue = stop_queue
        self.retry_after = retry_after


def _title_is_english(title):
    if not isinstance(title, str) or not title.strip():
        return False
    if re.search(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]", title):
        return False
    if re.search(r"[\u0400-\u04ff]", title):
        return False
    if re.search(r"[\u0370-\u03ff]", title):
        return False
    if re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]", title):
        return False
    return bool(re.search(r"[A-Za-z]", title))


def _validate_english_title(result):
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return result
    for category_data in data.values():
        if isinstance(category_data, dict) and "title" in category_data:
            title = category_data["title"]
            if title is not None and not _title_is_english(title):
                raise ValueError("AI title is not English")
    return result


def _english_fallback_title(category, category_data):
    if not isinstance(category_data, dict):
        return None
    if category == "housinglist":
        listing_type = str(category_data.get("listing_type") or "rent").strip().lower()
        action = "Room for Rent" if listing_type == "roommate" else "Property for Rent"
        bedrooms = category_data.get("bedrooms")
        property_type = str(category_data.get("property_type") or "property").strip()
        city = str(category_data.get("city") or "").strip()
        if bedrooms is not None and str(bedrooms).strip():
            action = f"{bedrooms}-Bedroom {property_type.title()} for Rent" if listing_type != "roommate" else f"{bedrooms}-Bedroom Room for Rent"
        elif listing_type != "roommate":
            action = f"{property_type.title()} for Rent"
        return f"{action} in {city}" if city else action
    if category == "joblist":
        job_title = str(category_data.get("job_title") or category_data.get("position") or "Job Opportunity").strip()
        city = str(category_data.get("city") or "").strip()
        return f"{job_title} in {city}" if city else job_title
    if category == "transferlist":
        origin = str(category_data.get("origin_city") or "").strip()
        destination = str(category_data.get("destination_city") or "").strip()
        if origin and destination:
            return f"Air Cargo Request from {origin} to {destination}"
        if origin or destination:
            city = origin or destination
            return f"Air Cargo Request for {city}"
        return "Air Cargo Request"
    return "Marketplace Listing"


def _fallback_title(source_text):
    if not isinstance(source_text, str):
        return None
    lines = [re.sub(r"\s+", " ", line).strip() for line in source_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None
    title = re.sub(r"https?://\S+|www\.\S+", "", lines[0], flags=re.IGNORECASE)
    title = re.sub(r"#[\w-]+", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:200] if _title_is_english(title) else None


def _ensure_titles(result, source_text):
    if not isinstance(result, dict):
        return result
    category = result.get("category")
    data = result.get("data")
    if category not in CATEGORIES or not isinstance(data, dict):
        return result
    category_data = data.get(category)
    if not isinstance(category_data, dict):
        return result
    title = category_data.get("title")
    if isinstance(title, str) and title.strip() and _title_is_english(title):
        category_data["title"] = title.strip()[:200]
        return result
    fallback = _fallback_title(source_text)
    if fallback:
        category_data["title"] = fallback
        return result
    generated = _english_fallback_title(category, category_data)
    if generated:
        category_data["title"] = generated[:200]
    return result


def _normalize_selected_category_data(result):
    """Unwrap only a singleton list for the authoritative category."""
    if not isinstance(result, dict):
        return result
    category = result.get("category")
    if category not in CATEGORIES:
        return result
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    category_data = data.get(category)
    if isinstance(category_data, list) and len(category_data) == 1 and isinstance(category_data[0], dict):
        data[category] = category_data[0]
    return result


def _normalize_defaults(result):
    if not isinstance(result, dict):
        return result
    category = result.get("category")
    data = result.get("data")
    if not isinstance(data, dict) or category not in data or not isinstance(data[category], dict):
        return result
    category_data = data[category]
    if category == "housinglist":
        category_data.setdefault("country_code", "CA")
        category_data.setdefault("currency", "CAD")
    elif category == "transferlist":
        category_data.setdefault("currency", "CAD")
    elif category == "joblist" and category_data.get("salary") is not None:
        category_data.setdefault("salary_currency", "CAD")
    return result


def _normalize_currencies(result):
    if not isinstance(result, dict):
        return result
    category = result.get("category")
    data = result.get("data")
    if not isinstance(data, dict) or category not in data or not isinstance(data[category], dict):
        return result
    category_data = data[category]
    if category in {"housinglist", "transferlist"}:
        currency = category_data.get("currency")
        if currency is not None and str(currency).strip().upper() != "CAD":
            category_data["price"] = None
            category_data["currency"] = None
    elif category == "joblist":
        currency = category_data.get("salary_currency")
        if currency is not None and str(currency).strip().upper() != "CAD":
            category_data["salary"] = None
            category_data["salary_currency"] = None
    return result


def __getattr__(name):
    # Keep the former public import available without coupling this shared
    # validation module to a concrete provider at import time.
    if name == "GroqExtractor":
        from ai.providers.groq import GroqClient
        return GroqClient
    raise AttributeError(name)


__all__ = [
    "AIExtractionError",
    "GroqExtractor",
    "_normalize_selected_category_data",
]
