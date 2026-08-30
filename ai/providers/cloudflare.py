"""Cloudflare Workers AI implementation of the Telclaw provider contract."""

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

logger = logging.getLogger("telclaw.ai.cloudflare")


class CloudflareProvider(AIProvider):
    """Cloudflare Workers AI adapter with prioritized credential failover."""

    name = "cloudflare"
    API_BASE = "https://api.cloudflare.com/client/v4/accounts"

    def __init__(self, account_id=None, api_token=None, model=None, timeout=None, rate_limiter=None, categories=None, providers=None):
        if providers is None and any(value is not None for value in (account_id, api_token, model)):
            providers = [{"account_id": account_id, "api_token": api_token, "model": model}]
        providers = list(providers if providers is not None else config.CLOUDFLARE_PROVIDERS)[:3]
        if not providers:
            raise RuntimeError("No Cloudflare AI credentials are configured")
        for index, item in enumerate(providers, start=1):
            missing = [name for name, value in (("account_id", item.get("account_id")), ("api_token", item.get("api_token")), ("model", item.get("model"))) if not value]
            if missing:
                raise RuntimeError(f"Cloudflare credential #{index} is incomplete; missing {', '.join(missing)}")
        self.providers = providers
        self.active_index = 0
        self._unavailable_until = [0.0] * len(providers)
        self.timeout = timeout if timeout is not None else config.CLOUDFLARE_TIMEOUT_SECONDS
        self.rate_limiter = rate_limiter or RateLimiter(config.CLOUDFLARE_REQUESTS_PER_MINUTE)
        self.categories = tuple(categories or CLASSIFICATION_CATEGORIES)

    @property
    def _active_credentials(self):
        return self.providers[self.active_index]

    @property
    def account_id(self):
        return self._active_credentials["account_id"]

    @property
    def api_token(self):
        return self._active_credentials["api_token"]

    @property
    def model(self):
        return self._active_credentials["model"]

    @property
    def endpoint(self):
        return f"{self.API_BASE}/{self.account_id}/ai/run/{self.model}"

    @staticmethod
    def _reason(status):
        if status == 401:
            return "invalid_api_key"
        if status == 403:
            return "permissions_error"
        if status == 404:
            return "model_not_found"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "server_error"
        return "provider_error"

    @staticmethod
    def _retry_after(headers, detail):
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                pass
        match = re.search(r"retry(?:[- ]after)?[:=\s]+(\d+(?:\.\d+)?)\s*s?", detail or "", re.I)
        return float(match.group(1)) if match else None

    def _next_available(self):
        now = time.monotonic()
        for index in range(self.active_index + 1, len(self.providers)):
            if self._unavailable_until[index] <= now:
                return index
        return None

    def _request(self, messages):
        attempts = 0
        while attempts <= config.AI_RETRY_COUNT:
            now = time.monotonic()
            if self._unavailable_until[self.active_index] > now:
                next_index = self._next_available()
                if next_index is None:
                    time.sleep(max(0.0, self._unavailable_until[self.active_index] - now))
                else:
                    self.active_index = next_index
            payload = {"messages": messages, "temperature": 0, "response_format": {"type": "json_object"}}
            try:
                self.rate_limiter.wait()
                with self.rate_limiter.slot():
                    response = requests.post(self.endpoint, headers={"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                self._unavailable_until[self.active_index] = time.monotonic() + config.AI_COOLDOWN_SECONDS
                next_index = self._next_available()
                if next_index is not None:
                    self.active_index = next_index
                    attempts += 1
                    continue
                raise AIExtractionError("Cloudflare request failed: transport error", provider=self.name, reason="network_error") from exc

            if not response.ok:
                detail = response.text.strip()
                status = response.status_code
                reason = self._reason(status)
                retry_after = self._retry_after(response.headers, detail) if status == 429 else None
                logger.error("Cloudflare provider error: status=%s model=%s reason=%s", status, self.model, reason)
                next_index = self._next_available() if status in {401, 403, 429, 500, 502, 503, 504} else None
                if next_index is not None:
                    cooldown = retry_after if retry_after is not None else config.AI_COOLDOWN_SECONDS
                    self._unavailable_until[self.active_index] = time.monotonic() + cooldown
                    self.active_index = next_index
                    attempts += 1
                    continue
                raise AIExtractionError(
                    f"Cloudflare request failed: status={status}; model={self.model}; reason={reason}",
                    provider=self.name, status=status, reason=reason,
                    stop_queue=status == 403, retry_after=retry_after,
                )

            try:
                body = response.json()
                if isinstance(body, dict) and body.get("success") is False:
                    raise ValueError("Cloudflare returned an unsuccessful response")
                result = body.get("result") if isinstance(body, dict) else None
                if isinstance(result, dict):
                    content = result.get("response") or result.get("content")
                else:
                    content = result
                if isinstance(content, dict):
                    return content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Cloudflare response contains no JSON output")
                return json.loads(content)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise AIExtractionError("Invalid Cloudflare JSON output", provider=self.name, reason="invalid_provider_output") from exc
        raise RuntimeError("Cloudflare provider retry budget exhausted")

    def classify_batch(self, messages):
        payload_messages = [{"message_id": int(item["message_id"]), "text": str(item.get("text") or "").strip()} for item in messages if str(item.get("text") or "").strip()]
        if not payload_messages:
            return {}
        requested_ids = [item["message_id"] for item in payload_messages]
        result = self._request([
            {"role": "system", "content": build_classification_prompt(self.categories)},
            {"role": "user", "content": json.dumps({"messages": payload_messages}, ensure_ascii=False)},
        ])
        try:
            return validate_classification_result(result, requested_ids, self.categories)
        except ValueError as exc:
            if not str(exc).startswith("missing classification for message_id(s):"):
                raise AIExtractionError(f"Invalid Cloudflare classification JSON output: {exc}", provider=self.name, reason="invalid_provider_output") from exc
            recovered = {}
            for item in result.get("classifications", []) if isinstance(result, dict) else []:
                try:
                    message_id, category = int(item["message_id"]), str(item["category"]).strip().lower()
                    if message_id in requested_ids and category in self.categories:
                        recovered[message_id] = category
                except (KeyError, TypeError, ValueError):
                    continue
            by_id = {item["message_id"]: item for item in payload_messages}
            for message_id in (item for item in requested_ids if item not in recovered):
                single = self.classify_batch([by_id[message_id]])
                recovered.update(single)
            return recovered

    def extract(self, text, category):
        if category not in CATEGORIES:
            raise AIExtractionError(f"Invalid authoritative extraction category: {category}", provider=self.name, reason="invalid_category")
        if not isinstance(text, str) or not text.strip():
            raise AIExtractionError("Cannot call Cloudflare with empty text", provider=self.name, reason="invalid_input")
        result = self._request([{"role": "system", "content": render_prompt(category, text.strip())}])
        try:
            if not isinstance(result, dict) or result.get("category") != category:
                raise ValueError(f"Extraction category mismatch: expected={category} received={result.get('category') if isinstance(result, dict) else None}")
            result = _normalize_selected_category_data(result)
            result = _ensure_titles(result, text)
            result = _normalize_defaults(result)
            result = _normalize_currencies(result)
            result = _validate_english_title(result)
            validated_category, validated_data = validate_result(result)
            return {"category": validated_category, "data": {validated_category: validated_data}}
        except AIExtractionError:
            raise
        except (ValueError, TypeError) as exc:
            raise AIExtractionError(f"Invalid Cloudflare JSON output: {exc}", provider=self.name, reason="invalid_provider_output") from exc


__all__ = ["CloudflareProvider"]
