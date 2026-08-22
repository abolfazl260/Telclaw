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

    def __init__(self, message, *, status=None, reason=None, provider="groq", stop_queue=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.provider = provider
        self.stop_queue = stop_queue
        self.retry_after = retry_after


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
        city = str(category_data.get("city") or category_data.get("from_city") or "").strip()
        return f"Property Transfer Opportunity in {city}" if city else "Property Transfer Opportunity"
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
    """Groq client using the OpenAI-compatible HTTP API with three-key failover."""

    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key=None, model=None, timeout=60, rate_limiter=None):
        # Explicit api_key/model arguments remain backward compatible and mean
        # "use this single provider". The normal path uses configured providers.
        if api_key:
            self.providers = [{"api_key": api_key, "model": model or config.GROQ_MODEL}]
        else:
            self.providers = list(getattr(config, "GROQ_PROVIDERS", [{"api_key": config.GROQ_API_KEY, "model": config.GROQ_MODEL}]))[:3]
        self.timeout = timeout
        self.rate_limiter = rate_limiter or RateLimiter(requests_per_minute=config.GROQ_REQUESTS_PER_MINUTE)
        self._provider_state = {
            index: {"available_at": 0.0, "rate_limited": False}
            for index in range(len(self.providers))
        }

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

    def _recover_providers(self):
        now = time.monotonic()
        for index, state in self._provider_state.items():
            if state["rate_limited"] and now >= state["available_at"]:
                state["rate_limited"] = False
                state["available_at"] = 0.0
                logger.info("[AI] API #%s recovered", index + 1)

    def _available_provider_indices(self):
        self._recover_providers()
        return [
            index for index in range(len(self.providers))
            if not self._provider_state[index]["rate_limited"]
        ]

    def _mark_rate_limited(self, index, wait_seconds):
        wait_seconds = max(0.0, float(wait_seconds))
        state = self._provider_state[index]
        state["available_at"] = time.monotonic() + wait_seconds
        state["rate_limited"] = True

    def _request(self, provider_index, payload):
        provider = self.providers[provider_index]
        self.rate_limiter.wait()
        with self.rate_limiter.slot():
            return requests.post(
                self.ENDPOINT,
                headers={"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )

    def extract(self, processed_text):
        if not self.providers or not self.providers[0].get("api_key"):
            raise AIExtractionError("GROQ_API_KEY is not configured", reason="missing_api_key")
        if not isinstance(processed_text, str) or not processed_text.strip():
            raise AIExtractionError("Cannot call Groq with empty text", reason="invalid_input")

        last_error = None
        attempts = 0
        max_attempts = max(1, config.GROQ_RATE_LIMIT_MAX_RETRIES + len(self.providers))

        while attempts < max_attempts:
            available = self._available_provider_indices()
            if not available:
                raise last_error or AIExtractionError("All Groq APIs are temporarily unavailable", reason="rate_limit")
            provider_index = available[0]
            provider = self.providers[provider_index]
            model = provider["model"]

            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _build_extraction_prompt()},
                    {"role": "user", "content": processed_text.strip()},
                ],
                "temperature": 0,
                "max_completion_tokens": config.GROQ_MAX_COMPLETION_TOKENS,
                "response_format": {"type": "json_object"},
            }
            if model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
                payload["include_reasoning"] = False

            try:
                response = self._request(provider_index, payload)
            except requests.RequestException as exc:
                raise AIExtractionError(
                    f"Groq request failed: provider=groq; model={model}; transport={exc}",
                    reason="network_error",
                ) from exc

            if response.status_code == 429:
                detail = response.text.strip()
                retry_after = self._parse_retry_after(response.headers, detail)
                if retry_after is None:
                    retry_after = min(
                        config.GROQ_RATE_LIMIT_MAX_WAIT_SECONDS,
                        config.GROQ_RATE_LIMIT_MIN_WAIT_SECONDS * (2 ** min(attempts, 5)),
                    )

                logger.warning("[AI] API #%s rate limited", provider_index + 1)
                logger.warning("[AI] Required wait: %.0fs", retry_after)

                if retry_after > config.GROQ_FAILOVER_THRESHOLD_SECONDS and provider_index < len(self.providers) - 1:
                    self._mark_rate_limited(provider_index, retry_after)
                    logger.warning("[AI] Wait exceeds %.0fs", config.GROQ_FAILOVER_THRESHOLD_SECONDS)
                    logger.warning("[AI] Switching to API #%s", provider_index + 2)
                    last_error = AIExtractionError(
                        "Groq rate limit exceeded; failing over to next API",
                        status=429,
                        reason="rate_limit",
                        retry_after=retry_after,
                    )
                    attempts += 1
                    continue

                if retry_after <= config.GROQ_FAILOVER_THRESHOLD_SECONDS:
                    logger.info("[AI] API #%s retrying after %.0fs", provider_index + 1, retry_after)
                    time.sleep(retry_after)
                    attempts += 1
                    continue

                self._mark_rate_limited(provider_index, retry_after)
                last_error = AIExtractionError(
                    "Groq rate limit exceeded on final API",
                    status=429,
                    reason="rate_limit",
                    retry_after=retry_after,
                )
                attempts += 1
                continue

            if not response.ok:
                detail = response.text.strip()
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                reason = self._provider_reason(response.status_code, detail)
                logger.error(
                    "Groq provider error: provider=groq status=%s model=%s reason=%s request_id=%s response=%s",
                    response.status_code, model, reason, request_id or "<none>", detail[:4000] or "<empty>",
                )
                diagnostic = [
                    "provider=groq", f"status={response.status_code}", f"model={model}",
                    f"reason={reason}", f"response={detail[:4000] or '<empty>'}",
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
                result = _normalize_defaults(result)
                result = _normalize_currencies(result)
                result = _validate_english_title(result)
                return validate_result(result)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise AIExtractionError(f"Invalid Groq JSON output: {exc}", reason="invalid_provider_output") from exc

        raise last_error or AIExtractionError("Groq extraction failed after retries", reason="provider_error")
