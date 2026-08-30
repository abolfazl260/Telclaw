"""Groq implementation of the provider contract."""

from __future__ import annotations

import json
import logging
import re
import time

import requests

import config
from ai.category_schemas import CATEGORIES, CLASSIFICATION_CATEGORIES, validate_result
from ai.category_classifier import build_classification_prompt, validate_classification_result
from ai.extractor import AIExtractionError, _ensure_titles, _normalize_currencies, _normalize_defaults, _normalize_selected_category_data, _validate_english_title
from ai.prompt_loader import render_prompt
from ai.rate_limiter import RateLimiter
from ai.providers.base import AIProvider

logger = logging.getLogger("telclaw.ai")
classification_logger = logging.getLogger("telclaw.ai.classification")


class GroqClient:
    """Single Groq credential client."""

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


class _GroqBatchClassifier:
    ENDPOINT = GroqClient.ENDPOINT

    def __init__(self, api_key=None, model=None, timeout=60, rate_limiter=None, categories=None):
        self.api_key = api_key or config.GROQ_API_KEY
        self.model = model or config.GROQ_MODEL
        self.timeout = timeout
        self.rate_limiter = rate_limiter or RateLimiter(requests_per_minute=config.GROQ_REQUESTS_PER_MINUTE)
        self.categories = tuple(categories or CLASSIFICATION_CATEGORIES)

    def _request_batch(self, payload_messages):
        """Send one provider request and return the raw parsed JSON result."""
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
            reason = GroqClient._provider_reason(response.status_code, detail)
            retry_after = GroqClient._parse_retry_after(response.headers, detail) if response.status_code == 429 else None
            logger.error(
                "Groq classification error: status=%s model=%s reason=%s response=%s",
                response.status_code, self.model, reason, detail[:4000],
            )
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
            return json.loads(output_text)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AIExtractionError(f"Invalid Groq classification JSON output: {exc}", reason="invalid_provider_output") from exc

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

        requested_ids = [item["message_id"] for item in payload_messages]
        result = self._request_batch(payload_messages)

        try:
            return validate_classification_result(result, requested_ids, self.categories)
        except ValueError as exc:
            # Groq can occasionally return a valid JSON object but omit one or
            # more IDs. Do not discard the valid classifications. Reclassify
            # only the missing messages in smaller batches, which also reduces
            # the chance of another truncated/partial provider response.
            if not str(exc).startswith("missing classification for message_id(s):"):
                raise AIExtractionError(f"Invalid Groq classification JSON output: {exc}", reason="invalid_provider_output") from exc

            categories = set(self.categories)
            items = result.get("classifications") if isinstance(result, dict) else None
            recovered = {}
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        message_id = int(item.get("message_id"))
                        category = str(item.get("category") or "").strip().lower()
                    except (TypeError, ValueError):
                        continue
                    if message_id in set(requested_ids) and category in categories:
                        recovered[message_id] = category

            missing_ids = [message_id for message_id in requested_ids if message_id not in recovered]
            if not missing_ids:
                return recovered

            by_id = {item["message_id"]: item for item in payload_messages}
            logger.warning(
                "[AI CLASSIFICATION PARTIAL OUTPUT] provider omitted message_id(s)=%s; retrying missing items individually",
                missing_ids,
            )

            for message_id in missing_ids:
                single_result = self._request_batch([by_id[message_id]])
                single = validate_classification_result(single_result, [message_id], self.categories)
                recovered.update(single)

            return recovered


class GroqProvider(AIProvider):
    """Groq provider retaining the existing prioritized multi-key behavior."""

    name = "groq"

    def __init__(self, providers=None):
        providers = list(providers if providers is not None else config.GROQ_PROVIDERS)[:3]
        if not providers:
            raise RuntimeError("No Groq AI providers are configured")
        self.providers = [GroqClient(api_key=item["api_key"], model=item["model"]) for item in providers]
        self.classifiers = [
            _GroqBatchClassifier(api_key=item["api_key"], model=item["model"])
            for item in providers
        ]
        self._unavailable_until = [0.0] * len(self.providers)
        self.active_index = 0
        self._select_highest_priority_available(announce=True, reason="startup")

    @property
    def active_provider(self):
        return f"groq-{self.active_index + 1}"

    @property
    def provider(self):
        return self.active_provider

    @property
    def model(self):
        return self.providers[self.active_index].model

    def _announce_active_provider(self, reason):
        message = f"[AI] Active provider: {self.active_provider} | model={self.model} | reason={reason}"
        print(message)
        logger.info(message)

    def _select_highest_priority_available(self, announce=False, reason="recovery"):
        now = time.monotonic()
        for index, available_at in enumerate(self._unavailable_until):
            if available_at <= now:
                changed = index != self.active_index
                self.active_index = index
                if announce and (changed or reason == "startup"):
                    self._announce_active_provider(reason)
                elif changed:
                    self._announce_active_provider(reason)
                return index
        return None

    def _mark_unavailable(self, index, wait_seconds):
        self._unavailable_until[index] = time.monotonic() + max(0.0, float(wait_seconds))

    def _switch_to_next_available(self):
        now = time.monotonic()
        for index in range(self.active_index + 1, len(self.providers)):
            if self._unavailable_until[index] <= now:
                old = self.active_provider
                self.active_index = index
                print(f"[AI] Switching provider: {old} -> {self.active_provider} | model={self.model} | reason=rate_limit")
                logger.warning("[AI PROVIDER SWITCH] from=%s to=%s model=%s reason=rate_limit", old, self.active_provider, self.model)
                return True
        return False

    def _call_with_rotation(self, operation, *args, **kwargs):
        while True:
            self._select_highest_priority_available(reason="recovery")
            try:
                return operation(self.active_index, *args, **kwargs)
            except AIExtractionError as exc:
                if exc.status != 429 or exc.retry_after is None or exc.retry_after <= config.GROQ_FAILOVER_THRESHOLD_SECONDS:
                    raise
                self._mark_unavailable(self.active_index, exc.retry_after)
                if self._switch_to_next_available():
                    continue
                raise

    def extract(self, text, category):
        if category is None:
            raise AIExtractionError("An authoritative extraction category is required", reason="invalid_category")
        return self._call_with_rotation(lambda index, value, selected: self.providers[index].extract(value, category=selected), text, category)

    def classify_batch(self, messages):
        return self._call_with_rotation(lambda index, batch: self.classifiers[index].classify_batch(batch), messages)


__all__ = ["GroqProvider", "GroqClient"]
