"""Three-key Groq provider with rate-limit failover and recovery."""

import logging
import time

import config
from ai.extractor import AIExtractionError, GroqExtractor

logger = logging.getLogger("telclaw.ai")


class GroqProviderFailover:
    """Routes requests across up to three configured Groq API keys.

    A provider is switched only for HTTP 429 when the required wait is greater
    than the configured failover threshold. Short waits are returned to the
    existing AI retry policy so that the current provider is retried normally.
    Providers recover automatically and API #1 regains priority after recovery.
    """

    def __init__(self, providers=None):
        if providers is None:
            providers = config.GROQ_PROVIDERS
        if not providers:
            raise RuntimeError("No Groq AI providers are configured")

        self.providers = [
            GroqExtractor(
                api_key=item["api_key"],
                model=item["model"],
            )
            for item in list(providers)[:3]
        ]
        self._unavailable_until = [0.0] * len(self.providers)
        self.active_index = 0
        self._select_highest_priority_available(announce=True, reason="startup")

    @property
    def active_provider(self):
        return f"groq-{self.active_index + 1}"

    @property
    def model(self):
        return self.providers[self.active_index].model

    @property
    def provider(self):
        return self.active_provider

    def _announce_active_provider(self, reason):
        provider = self.active_provider
        model = self.model
        message = f"[AI] Active provider: {provider} | model={model} | reason={reason}"
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
                new = self.active_provider
                print(f"[AI] Switching provider: {old} -> {new} | model={self.model} | reason=rate_limit")
                logger.warning(
                    "[AI PROVIDER SWITCH] from=%s to=%s model=%s reason=rate_limit",
                    old,
                    new,
                    self.model,
                )
                return True
        return False

    def extract(self, processed_text):
        while True:
            # Recovery is checked before every request. The highest-priority
            # recovered key becomes active again, so API #1 always regains
            # priority after its cooldown expires.
            self._select_highest_priority_available(reason="recovery")
            extractor = self.providers[self.active_index]
            try:
                return extractor.extract(processed_text)
            except AIExtractionError as exc:
                if exc.status != 429:
                    raise

                retry_after = exc.retry_after
                if retry_after is None:
                    # The existing AI service owns exponential backoff when the
                    # provider did not provide a concrete wait value. Do not
                    # rotate keys merely because a wait value is unknown.
                    raise

                print(
                    f"[AI] API #{self.active_index + 1} rate limited | "
                    f"Required wait: {retry_after:.0f}s"
                )
                logger.warning(
                    "[AI RATE LIMIT] provider=%s model=%s wait=%.2fs threshold=%.2fs",
                    self.active_provider,
                    self.model,
                    retry_after,
                    config.GROQ_FAILOVER_THRESHOLD_SECONDS,
                )

                if retry_after <= config.GROQ_FAILOVER_THRESHOLD_SECONDS:
                    # Keep the same API. AIProcessingService will perform its
                    # existing retry/backoff without blocking this failover layer.
                    raise

                current = self.active_index
                self._mark_unavailable(current, retry_after)
                print(f"[AI] Wait exceeds {config.GROQ_FAILOVER_THRESHOLD_SECONDS:.0f}s")

                if self._switch_to_next_available():
                    print(f"[AI] Switching to API #{self.active_index + 1}")
                    continue

                # No later provider is available. Keep the current 429 so the
                # existing retry policy can wait and retry after the cooldown.
                raise


__all__ = ["GroqProviderFailover"]
