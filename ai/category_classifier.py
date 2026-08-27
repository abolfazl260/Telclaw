"""Batch AI category classification for cleaned Telclaw messages."""

from __future__ import annotations

import json
import logging
import requests

import config
from ai.category_schemas import CLASSIFICATION_CATEGORIES
from ai.extractor import AIExtractionError, GroqExtractor
from ai.rate_limiter import RateLimiter

logger = logging.getLogger("telclaw.ai.classification")


def build_classification_prompt(categories=None):
    categories = tuple(categories or CLASSIFICATION_CATEGORIES)
    return (
        "Classify each cleaned Telegram marketplace message into exactly one category.\n"
        f"Valid categories: {', '.join(categories)}.\n\n"
        "Category guide:\n"
        "- housinglist: rental housing, rooms, roommates, properties, apartments, condos, basements, studios.\n"
        "- transferlist: air-cargo, passenger baggage, luggage space, parcel/package carried by airline passenger, flight-based shipping.\n"
        "- joblist: job offers, hiring, work requests, employment, services offered as work.\n"
        "- none: irrelevant, unclear, spam, news, discussion, or not a marketplace listing.\n\n"
        "Return ONLY JSON with this shape:\n"
        '{"classifications":[{"message_id":101,"category":"housinglist|transferlist|joblist|none"}]}\n'
        "Do not include explanations. Keep every input message_id exactly as provided."
    )


def validate_classification_result(result, requested_ids, categories=None):
    categories = set(categories or CLASSIFICATION_CATEGORIES)
    requested_ids = [int(message_id) for message_id in requested_ids]
    requested_set = set(requested_ids)
    if not isinstance(result, dict):
        raise ValueError("classification result must be an object")
    items = result.get("classifications")
    if not isinstance(items, list):
        raise ValueError("classification result must contain classifications list")
    parsed = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("classification item must be an object")
        message_id = int(item.get("message_id"))
        category = str(item.get("category") or "").strip().lower()
        if message_id not in requested_set:
            raise ValueError(f"unexpected message_id in classification result: {message_id}")
        if category not in categories:
            raise ValueError(f"unsupported classification category: {category!r}")
        parsed[message_id] = category
    missing = [message_id for message_id in requested_ids if message_id not in parsed]
    if missing:
        raise ValueError(f"missing classification for message_id(s): {missing}")
    return parsed


class GroqBatchCategoryClassifier:
    """Classify multiple cleaned messages in one Groq JSON-mode request."""

    ENDPOINT = GroqExtractor.ENDPOINT

    def __init__(self, api_key=None, model=None, timeout=60, rate_limiter=None, categories=None):
        self.api_key = api_key or config.GROQ_API_KEY
        self.model = model or config.GROQ_MODEL
        self.timeout = timeout
        self.rate_limiter = rate_limiter or RateLimiter(requests_per_minute=config.GROQ_REQUESTS_PER_MINUTE)
        self.categories = tuple(categories or CLASSIFICATION_CATEGORIES)

    def classify_batch(self, messages):
        if not self.api_key:
            raise AIExtractionError("GROQ_API_KEY is not configured", reason="missing_api_key")
        payload_messages = [
            {"message_id": int(item["message_id"]), "text": str(item.get("text") or "").strip()}
            for item in messages
            if str(item.get("text") or "").strip()
        ]
        if not payload_messages:
            return {}

        self.rate_limiter.wait()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_classification_prompt(self.categories)},
                {"role": "user", "content": json.dumps({"messages": payload_messages}, ensure_ascii=False)},
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
            raise AIExtractionError(f"Groq classification request failed: {exc}", reason="network_error") from exc

        if not response.ok:
            detail = response.text.strip()
            reason = GroqExtractor._provider_reason(response.status_code, detail)
            retry_after = GroqExtractor._parse_retry_after(response.headers, detail) if response.status_code == 429 else None
            logger.error("Groq classification error: status=%s model=%s reason=%s response=%s", response.status_code, self.model, reason, detail[:4000])
            raise AIExtractionError(
                f"Groq classification failed: status={response.status_code}; model={self.model}; reason={reason}; response={detail[:4000] or '<empty>'}",
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
                raise ValueError(f"Model refused classification: {message['refusal']}")
            output_text = message.get("content")
            if not output_text:
                raise ValueError("No JSON output returned")
            result = json.loads(output_text)
            requested_ids = [item["message_id"] for item in payload_messages]
            return validate_classification_result(result, requested_ids, self.categories)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AIExtractionError(f"Invalid Groq classification JSON output: {exc}", reason="invalid_provider_output") from exc


__all__ = ["GroqBatchCategoryClassifier", "build_classification_prompt", "validate_classification_result"]
