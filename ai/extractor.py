"""AI extraction service using Groq JSON mode with local validation."""

import json
import logging
import re

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


def _build_extraction_prompt():
    category_fields = "\n".join(
        f"- {category}: {', '.join(fields)}" for category, fields in CATEGORY_FIELDS.items()
    )
    return f"""You extract structured marketplace information from processed Telegram messages.
Classify each message into exactly one category: {', '.join(CATEGORIES)}.
Extract only facts explicitly supported by the message. Never invent values.
Unknown scalar values must be null. Unknown list values must be [].
Use normalized English field names. Keep the original meaning and do not copy Telegram metadata.

TITLE REQUIREMENT:
- The title MUST always be written in English.
- If the source message is not English, translate the title meaning into natural English.
- Do not transliterate the original-language title.
- Do not return the original-language title.
- Keep the English title concise and suitable for a marketplace listing.
- Do not put URLs, hashtags, emojis, or explanations in the title.

CURRENCY REQUIREMENT:
- The platform's canonical currency is ALWAYS Canadian dollars (CAD).
- For every listing/request, the canonical monetary currency must be CAD.
- Use "CAD" for the housing/transfer currency field and for job salary_currency.
- If the source amount is explicitly in another currency, do NOT invent an exchange rate. Preserve the amount only if it is already reliably CAD; otherwise set the monetary amount and currency field to null.
- Do not output USD, EUR, GBP, TRY, IRR, or any other currency as the canonical currency.

Return ONLY valid JSON. Do not use Markdown fences. Do not include explanations, comments, or <think> tags.
Return exactly this top-level structure:
{{"category":"housinglist|transferlist|joblist","data":{{"<selected_category>":{{...fields...}}}}}}
Only include the selected category inside data. Do not include the other categories.

Allowed fields by category:
{category_fields}

For the selected category, use the exact field names above. You may omit fields whose values are unknown; use null or [] when you include them.
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


def _normalize_currencies(result):
    """Enforce CAD as the canonical currency without inventing exchange rates."""
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
            result = _normalize_currencies(result)
            result = _validate_english_title(result)
            return validate_result(result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AIExtractionError(
                f"Invalid Groq JSON output: {exc}",
                reason="invalid_provider_output",
            ) from exc
