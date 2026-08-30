"""Provider-independent entry point for Telclaw AI operations."""

from __future__ import annotations

from ai.providers.groq import GroqProvider


class AIProviderManager:
    """Route AI operations to the configured provider.

    Only Groq is registered today; the manager intentionally keeps business
    services independent from the provider implementation.
    """

    def __init__(self, provider=None):
        self.provider_instance = provider or GroqProvider()

    @property
    def provider(self):
        return getattr(self.provider_instance, "provider", getattr(self.provider_instance, "name", "unknown"))

    @property
    def model(self):
        return getattr(self.provider_instance, "model", None)

    def classify_batch(self, messages):
        return self.provider_instance.classify_batch(messages)

    def extract(self, text, category):
        return self.provider_instance.extract(text, category)


__all__ = ["AIProviderManager"]
