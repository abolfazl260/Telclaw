"""Provider-agnostic entry point for Telclaw AI operations."""

from __future__ import annotations

import logging
import time

import config
from ai.extractor import AIExtractionError
from ai.providers.factory import create_provider


logger = logging.getLogger("telclaw.ai")


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
        self._last_failover_error = None
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
        # Recovery priority may be throttled, but cooldown eligibility may not:
        # even an already-selected provider can become temporarily unavailable.
        if (not force
                and self._unavailable_until[self.active_index] <= now
                and now - self._last_recovery_check < config.AI_RECOVERY_INTERVAL_SECONDS):
            return self.active_index
        self._last_recovery_check = now
        for index, available_at in enumerate(self._unavailable_until):
            if available_at <= now:
                self.active_index = index
                return index
        return None

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
        """Return the next available provider, wrapping to the first provider."""
        now = time.monotonic()
        provider_count = len(self.providers)
        if provider_count <= 1:
            return None

        # Continue in priority order, but wrap after the last provider so a
        # completed provider cycle can start again from provider #1.
        for offset in range(1, provider_count + 1):
            index = (self.active_index + offset) % provider_count
            if self._unavailable_until[index] <= now:
                return index
        return None

    def _cooldown_error(self, last_error=None):
        """Return the next eligibility time without calling a cooling provider."""
        remaining = max(0.0, min(self._unavailable_until) - time.monotonic())
        if last_error is not None and getattr(last_error, "status", None) != 429:
            # Preserve e.g. a permission error and its stop_queue semantics.
            return last_error
        return AIExtractionError(
            "All AI providers are temporarily unavailable (cooldown active)",
            status=429,
            reason="rate_limit",
            provider=getattr(last_error, "provider", self.provider),
            retry_after=remaining,
        )

    def _try_alternate_extraction_after_invalid_output(self, error, *args, **kwargs):
        """Try each available alternate exactly once for this extraction attempt.

        Bad JSON is a per-message failure, not a global provider outage. Keep
        the primary eligible for later messages and preserve its priority.
        Genuine failures (e.g. alternate rate limits) keep their own status
        and cooldown rather than being mislabeled as invalid JSON.
        """
        if len(self.providers) < 2:
            raise error

        last_error = error
        current = self.active_index
        now = time.monotonic()
        for offset in range(1, len(self.providers)):
            index = (current + offset) % len(self.providers)
            if self._unavailable_until[index] > now:
                continue
            candidate = self.providers[index]
            logger.warning(
                "[AI INVALID JSON FAILOVER] primary=%s alternate=%s",
                getattr(self.providers[current], "name", "unknown"),
                getattr(candidate, "name", "unknown"),
            )
            try:
                return candidate.extract(*args, **kwargs)
            except AIExtractionError as alternate_error:
                logger.warning(
                    "[AI EXTRACTION FALLBACK FAILED] provider=%s reason=%s status=%s",
                    getattr(candidate, "name", "unknown"),
                    getattr(alternate_error, "reason", None),
                    getattr(alternate_error, "status", None),
                )
                if getattr(alternate_error, "reason", None) != "invalid_provider_output":
                    if self._is_failover_error(alternate_error):
                        cooldown = (
                            getattr(alternate_error, "retry_after", None)
                            if getattr(alternate_error, "status", None) == 429
                            else None
                        )
                        if cooldown is not None and float(cooldown) <= 0:
                            cooldown = None
                        self._mark_unavailable(index, cooldown)
                    raise
                last_error = alternate_error
        raise last_error

    def _call(self, method, *args, allow_invalid_output_fallback=True, **kwargs):
        attempts = 0
        last_error = None
        while attempts <= config.AI_RETRY_COUNT:
            if self._select_highest_priority_available() is None:
                raise self._cooldown_error(self._last_failover_error)
            try:
                result = getattr(self.provider_instance, method)(*args, **kwargs)
                self._last_failover_error = None
                return result
            except AIExtractionError as exc:
                last_error = exc
                if (method == "extract" and allow_invalid_output_fallback
                        and getattr(exc, "reason", None) == "invalid_provider_output"):
                    return self._try_alternate_extraction_after_invalid_output(
                        exc, *args, **kwargs
                    )
                if not self._is_failover_error(exc):
                    raise

                current_index = self.active_index
                self._last_failover_error = exc
                cooldown = getattr(exc, "retry_after", None) if getattr(exc, "status", None) == 429 else None
                if cooldown is not None and float(cooldown) <= 0:
                    cooldown = None  # Prevent a 429 with retry_after=0 from hot-looping.
                self._mark_unavailable(current_index, cooldown)

                # Mark the failing provider BEFORE finding the next one, so a
                # wrapped search cannot immediately pick the same provider.
                next_index = self._next_available()
                if next_index is None:
                    # The upper Classification/Extraction retry handlers already
                    # implement bounded waiting on 429. Do not sleep or issue
                    # another HTTP request here.
                    if getattr(exc, "status", None) == 429:
                        raise self._cooldown_error(exc) from exc
                    raise

                wrapped = next_index <= current_index
                self.active_index = next_index
                if wrapped:
                    attempts += 1

        raise last_error

    def classify(self, text):
        return self._call("classify", text)

    def classify_batch(self, messages):
        return self._call("classify_batch", messages)

    def extract(self, text, category, *, allow_invalid_output_fallback=True):
        return self._call(
            "extract", text, category,
            allow_invalid_output_fallback=allow_invalid_output_fallback,
        )

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
