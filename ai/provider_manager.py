"""Provider-agnostic entry point for Telclaw AI operations."""

from __future__ import annotations

import time

import config
from ai.extractor import AIExtractionError
from ai.providers.factory import create_provider


class AIProviderManager:
    """Route AI operations using configured priority and safe failover."""

    def __init__(self, provider=None, provider_name=None):
        if provider is not None:
            self.provider_instance = provider
            self.providers = [provider]
            self.active_index = 0
        else:
            names = (provider_name,) if provider_name else config.AI_PROVIDERS
            self.providers = [create_provider(name) for name in names]
            if not self.providers:
                raise RuntimeError("No AI providers are configured")
            self.active_index = 0
        self._unavailable_until = [0.0] * len(self.providers)
        self._last_recovery_check = 0.0
        self._select_highest_priority_available(force=True)

    @property
    def provider_instance(self):
        return self.providers[self.active_index]

    @provider_instance.setter
    def provider_instance(self, value):
        self.providers = [value]
        self.active_index = 0

    @property
    def provider(self):
        instance = self.provider_instance
        return getattr(instance, "provider", getattr(instance, "name", "unknown"))

    @property
    def model(self):
        return getattr(self.provider_instance, "model", None)

    def _select_highest_priority_available(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_recovery_check < config.AI_RECOVERY_INTERVAL_SECONDS:
            return
        self._last_recovery_check = now
        for index, available_at in enumerate(self._unavailable_until):
            if available_at <= now:
                self.active_index = index
                return

    @staticmethod
    def _is_failover_error(exc):
        return getattr(exc, "reason", None) in {
            "invalid_api_key", "permissions_error", "model_blocked", "model_not_found",
            "rate_limit", "server_error", "network_error", "provider_error",
        } or getattr(exc, "status", None) in {401, 403, 429, 500, 502, 503, 504}

    def _mark_unavailable(self, index, cooldown=None):
        seconds = config.AI_COOLDOWN_SECONDS if cooldown is None else max(0.0, float(cooldown))
        self._unavailable_until[index] = time.monotonic() + seconds

    def _next_available(self):
        now = time.monotonic()
        for index in range(self.active_index + 1, len(self.providers)):
            if self._unavailable_until[index] <= now:
                return index
        return None

    def _call(self, method, *args, **kwargs):
        attempts = 0
        last_error = None
        while attempts <= config.AI_RETRY_COUNT:
            self._select_highest_priority_available()
            try:
                return getattr(self.provider_instance, method)(*args, **kwargs)
            except AIExtractionError as exc:
                last_error = exc
                if not self._is_failover_error(exc):
                    raise
                next_index = self._next_available()
                if next_index is not None:
                    cooldown = getattr(exc, "retry_after", None) if getattr(exc, "status", None) == 429 else None
                    self._mark_unavailable(self.active_index, cooldown)
                    self.active_index = next_index
                    continue
                if attempts >= config.AI_RETRY_COUNT:
                    raise
                self._mark_unavailable(self.active_index, getattr(exc, "retry_after", None))
                self._select_highest_priority_available(force=True)
                attempts += 1
        raise last_error

    def classify(self, text):
        return self._call("classify", text)

    def classify_batch(self, messages):
        return self._call("classify_batch", messages)

    def extract(self, text, category):
        return self._call("extract", text, category)

    def health_check(self):
        if hasattr(self.provider_instance, "health_check"):
            return self.provider_instance.health_check()
        return {"status": "unknown", "provider": self.provider}

    def get_status(self):
        # Intentionally expose only non-secret routing metadata.
        return {
            "provider": self.provider,
            "model": self.model,
            "priority": [getattr(item, "name", getattr(item, "provider", "unknown")) for item in self.providers],
            "active_index": self.active_index + 1,
            "configured_credentials": [self._credential_count(item) for item in self.providers],
        }

    @staticmethod
    def _credential_count(provider):
        if hasattr(provider, "providers"):
            return len(provider.providers)
        return 1


__all__ = ["AIProviderManager"]
