"""AI extraction service using Groq JSON mode with local validation."""

import json
import logging
import re
import time

import requests

import config
from ai.category_schemas import CATEGORY_FIELDS, CATEGORIES, validate_result
from ai.rate_limiter import RateLimiter

logger = logging.getLogger("telclaw.ai")


class AIExtractionError(RuntimeError):
    """An AI failure whose details are diagnostic-only and must not reach message storage."""

    def __init__(self, message, *, status=None, reason=None, provider="groq", stop_queue=False):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.provider = provider
        self.stop_queue = stop_queue


def _build_extraction_prompt(compact=False):
    if compact:
        return """Extract structured marketplace data from this Telegram message.
Return ONLY one valid JSON object, no Markdown, no explanation, no <think> tags.
Top level: {\"category\":\"housinglist|transferlist|joblist\",\"data\":{\"selected_category\":{...}}}.
Include ONLY fields you can determine plus the required fields below. Do not output empty optional fields.

Universal rules:
- title must be concise natural English. Translate it when necessary; never transliterate it.
- Never invent price, bedrooms, property type, or other facts.
- Canonical currency is CAD.
- For housing, if country is not stated, country_code=CA. Infer province from the stated city/neighborhood when it is unambiguous.
- For housing, output these required fields: listing_type, property_type, bedrooms, price, currency, country_code, province, city, title.
- Housing currency must be CAD and country_code must be CA. If price/bedrooms/property type/city/province cannot be reliably determined, use null and the local validator will reject the record.
- Housing bedrooms must be one of: 0, 1, 2, 3, 4+ (string).
- Housing property_type: apartment, condo, basement, studio, room, house.
- Housing listing_type: rent or roommate.
- Housing price is a monthly CAD number from 100 to 10000.

Message follows:
"""

    category_fields = "\n".join(
        f"- {category}: {', '.join(fields)}" for category, fields in CATEGORY_FIELDS.items()
    )
    return f"""You extract structured marketplace information from processed Telegram messages.
Classify each message into exactly one category: {', '.join(CATEGORIES)}.
Return ONLY valid JSON. No Markdown fences, explanations, comments, or <think> tags.
Never invent facts. Omit optional fields when unknown. Use normalized English field names.

Required output shape:
{{"category":"housinglist|transferlist|joblist","data":{{"selected_category":{{...}}}}}}
Only include the selected category and only fields with useful values.

TITLE: every returned title MUST be natural English. Translate non-English text into English; do not transliterate. Keep it concise and marketplace-ready. No URLs, hashtags, emojis, or explanations.

CURRENCY: the platform canonical currency is CAD. For housing, transfer and job salary fields, use CAD. If a source explicitly gives a non-CAD currency, do not invent an exchange rate; leave the monetary value/currency unavailable so validation can reject it.

HOUSING: when country is not stated, country_code defaults to CA. Infer province from the city/neighborhood when the location is unambiguous. Required for housing: listing_type, property_type, bedrooms, price, currency, country_code, province, city, title. Do not guess price, bedrooms, property type, city, or province.
Housing listing_type = rent|roommate; property_type = apartment|condo|basement|studio|room|house; bedrooms = 0|1|2|3|4+ as a string; price = monthly CAD number 100-10000.

Allowed fields:
{category_fields}
"""


def _title_is_english(title):
    """Reject titles containing characters from common non-Latin scripts."""
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
    """Validate every returned marketplace title without changing its content."""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return result
    for category_data in data.values():
        if isinstance(category_data, dict) and "title" in category_data:
            title = category_data["title"]
            if title is not None and not _title_is_english(title):
                raise ValueError("AI title is not English")
    return result


def _normalize_defaults(result):
    """Apply safe platform defaults before strict local validation."""
    if not isinstance(result, dict):
        return result
    category = result.get("category")
    data = result.get("data")
    if not isinstance(data, dict) or category not in data or not isinstance(data[category], dict):
        return result

    category_data = data[category]
    if category == "housinglist":
        # Advertio is Canada-only. A missing country is therefore safely CA.
        if not str(category_data.get("country_code") or "").strip():
            category_data["country_code"] = "CA"
        if not str(category_data.get("currency") or "").strip():
            category_data["currency"] = "CAD"
    elif category == "transferlist":
        if not str(category_data.get("currency") or "").strip():
            category_data["currency"] = "CAD"
    elif category == "joblist":
        if not str(category_data.get("salary_currency") or "").strip() and category_data.get("salary") is not None:
            category_data["salary_currency"] = "CAD"
    return result


def _normalize_currencies(result):
    """Reject explicit non-CAD currencies instead of silently converting them."""
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
        self.rate_limiter = rate_limiter or RateLimiter(
            requests_per_minute=config.GROQ_REQUESTS_PER_MINUTE
        )

    @staticmethod
    def _provider_reason(status, detail):
        text = (detail or "").lower()
        if status == 401:
            return "invalid_api_key"
        if status == 403:
            if "model" in text and ("block" in text or "permission" in text or "access" in text):
                return "model_blocked"
            return "permissions_error"
        if status == 404:
            return "model_not_found"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "server_error"
        return "provider_error"

    def _log_request_config(self):
        logger.warning("[DEBUG GROQ REQUEST] MODEL=%s ENDPOINT=%s", self.model, self.ENDPOINT)
        print(
            "\n[DEBUG GROQ REQUEST]\n"
            f"MODEL:\n{self.model}\n"
            f"ENDPOINT:\n{self.ENDPOINT}\n"
        )

    @staticmethod
    def _log_provider_error(status, model, reason, detail, request_id=None):
        response_detail = detail or "<empty>"
        diagnostic = (
            "\n[AI PROVIDER ERROR]\n"
            "Provider: Groq\n"
            f"HTTP Status: {status}\n"
            f"Model: {model}\n"
            f"Reason: {reason}\n"
            f"Response: {response_detail[:4000]}\n"
        )
        if request_id:
            diagnostic += f"Request ID: {request_id}\n"
        print(diagnostic)
        logger.error(
            "Groq provider error: status=%s model=%s reason=%s request_id=%s response=%s",
            status,
            model,
            reason,
            request_id or "<none>",
            response_detail[:4000],
        )

    def extract(self, processed_text):
        if not self.api_key:
            raise AIExtractionError("GROQ_API_KEY is not configured", reason="missing_api_key")
        if not isinstance(processed_text, str) or not processed_text.strip():
            raise AIExtractionError("Cannot call Groq with empty text", reason="invalid_input")

        self.rate_limiter.wait()
        self._log_request_config()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _build_extraction_prompt()},
                {"role": "user", "content": processed_text},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

        try:
            response = requests.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
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
            self._log_provider_error(
                response.status_code,
                self.model,
                reason,
                detail,
                request_id=request_id,
            )
            diagnostic = [
                "provider=groq",
                f"status={response.status_code}",
                f"model={self.model}",
                f"reason={reason}",
                f"response={detail[:4000] or '<empty>'}",
            ]
            if request_id:
                diagnostic.append(f"request_id={request_id}")
            raise AIExtractionError(
                "Groq request failed: " + "; ".join(diagnostic),
                status=response.status_code,
                reason=reason,
                stop_queue=response.status_code == 403,
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
            result = _ensure_titles(result, processed_text)
            result = _normalize_currencies(result)
            result = _validate_english_title(result)
            return validate_result(result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AIExtractionError(
                f"Invalid Groq JSON output: {exc}",
                reason="invalid_provider_output",
            ) from exc
