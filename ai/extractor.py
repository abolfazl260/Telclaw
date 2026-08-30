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


class GroqExtractor:
    """Groq client using the OpenAI-compatible HTTP API."""

    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key=None, model=None, timeout=60, rate_limiter=None):
        self.api_key = api_key or config.GROQ_API_KEY
        self.model = model or config.GROQ_MODEL
        self.timeout = timeout
        self.rate_limiter = rate_limiter or RateLimiter(requests_per_minute=config.GROQ_REQUESTS_PER_MINUTE)

    @staticmethod
    def _provider_reason(status, detail):
        text = (detail or "").lower()
        if status == 401:
            return "invalid_api_key"
        if status == 403:
            if "model" in text and any(x in text for x in ("block", "permission", "access")):
                return "model_blocked"
            return "permissions_error"
        if status == 404:
            return "model_not_found"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "server_error"
        return "provider_error"

    @staticmethod
    def _parse_retry_after(headers, detail):
        for name in ("retry-after", "Retry-After"):
            value = headers.get(name)
            if value:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    pass
        for name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            value = headers.get(name)
            if value:
                match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value))
                if match:
                    return max(0.0, float(match.group(1)))
        patterns = (
            r"try again in\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?",
            r"retry[- ]after[:=\s]+(\d+(?:\.\d+)?)\s*s?",
        )
        for pattern in patterns:
            match = re.search(pattern, detail or "", re.IGNORECASE)
            if match:
                if len(match.groups()) == 2:
                    return float(match.group(1) or 0) * 60 + float(match.group(2) or 0)
                return float(match.group(1))
        return None

    def extract(self, processed_text, category=None):
        if category not in CATEGORIES:
            raise AIExtractionError(f"Invalid authoritative extraction category: {category}", reason="invalid_category")
        if not self.api_key:
            raise AIExtractionError("GROQ_API_KEY is not configured", reason="missing_api_key")
        if not isinstance(processed_text, str) or not processed_text.strip():
            raise AIExtractionError("Cannot call Groq with empty text", reason="invalid_input")

        self.rate_limiter.wait()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": render_prompt(category, processed_text.strip())},
            ],
            "temperature": 0,
            "max_completion_tokens": config.GROQ_MAX_COMPLETION_TOKENS,
            "response_format": {"type": "json_object"},
        }
        if self.model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
            payload["include_reasoning"] = False

        try:
            with self.rate_limiter.slot():
                response = requests.post(
                    self.ENDPOINT,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise AIExtractionError(
                f"Groq request failed: provider=groq; model={self.model}; transport={exc}",
                reason="network_error",
            ) from exc

        if not response.ok:
            detail = response.text.strip()
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            reason = self._provider_reason(response.status_code, detail)
            retry_after = self._parse_retry_after(response.headers, detail) if response.status_code == 429 else None
            logger.error(
                "Groq provider error: provider=groq status=%s model=%s reason=%s request_id=%s retry_after=%s response=%s",
                response.status_code, self.model, reason, request_id or "<none>", retry_after if retry_after is not None else "<none>", detail[:4000] or "<empty>",
            )
            diagnostic = [
                "provider=groq", f"status={response.status_code}", f"model={self.model}",
                f"reason={reason}", f"response={detail[:4000] or '<empty>'}",
            ]
            if request_id:
                diagnostic.append(f"request_id={request_id}")
            if retry_after is not None:
                diagnostic.append(f"retry_after={retry_after:.3f}")
            raise AIExtractionError(
                "Groq request failed: " + "; ".join(diagnostic),
                status=response.status_code,
                reason=reason,
                stop_queue=response.status_code == 403,
                retry_after=retry_after,
            )

        try:
            response_data = response.json()
            choices = response_data.get("choices", [])
            if not choices:
                raise ValueError("No choices returned")
            message = choices[0].get("message", {})
            if message.get("refusal"):
                raise ValueError(f"Model refused extraction: {message['refusal']}")
            output_text = message.get("content")
            if not output_text:
                raise ValueError("No JSON output returned")
            result = json.loads(output_text)
            if not isinstance(result, dict):
                raise ValueError("Groq JSON output must be an object")
            if result.get("category") != category:
                raise AIExtractionError(f"Extraction category mismatch: expected={category} received={result.get('category')}", reason="category_mismatch")
            result = _normalize_selected_category_data(result)
            result = _ensure_titles(result, processed_text)
            result = _normalize_defaults(result)
            result = _normalize_currencies(result)
            result = _validate_english_title(result)

            # validate_result() returns (category, normalized_data). The service layer
            # consumes the canonical dict shape, so normalize the validator output here.
            validated_category, validated_data = validate_result(result)
            return {
                "category": validated_category,
                "data": {validated_category: validated_data},
            }
        except AIExtractionError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AIExtractionError(f"Invalid Groq JSON output: {exc}", reason="invalid_provider_output") from exc
