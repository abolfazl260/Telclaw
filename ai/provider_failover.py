"""Multi-key Groq provider with automatic rate-limit failover."""

import logging

import config
from ai.extractor import AIExtractionError, GroqExtractor

logger = logging.getLogger("telclaw.ai")


class GroqProviderFailover:
    """Routes requests across configured Groq API keys.

    A provider is switched only when Groq explicitly returns HTTP 429. Other
    errors remain associated with the current provider and are handled by the
    existing AI error policy.
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
            for item in providers
        ]
        self.active_index = 0
        self._announce_active_provider("startup")

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

    def _switch_provider(self):
        if self.active_index + 1 >= len(self.providers):
            return False
        old = self.active_provider
        self.active_index += 1
        new = self.active_provider
        print(f"[AI] Switching provider: {old} -> {new} | model={self.model} | reason=rate_limit")
        logger.warning(
            "[AI PROVIDER SWITCH] from=%s to=%s model=%s reason=rate_limit",
            old,
            new,
            self.model,
        )
        return True

    def extract(self, processed_text):
        while True:
            extractor = self.providers[self.active_index]
            try:
                return extractor.extract(processed_text)
            except AIExtractionError as exc:
                if exc.status != 429:
                    raise
                print(
                    f"[AI] Provider rate limit: {self.active_provider} | "
                    f"model={self.model} | status=429"
                )
                if not self._switch_provider():
                    print(
                        f"[AI] All configured providers are rate-limited; "
                        f"keeping active provider={self.active_provider}"
                    )
                    raise
                # Retry the same record immediately with the next API key.


__all__ = ["GroqProviderFailover"]
