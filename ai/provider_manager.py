"""Provider-independent entry point for Telclaw AI operations."""

from __future__ import annotations

import config

from ai.providers.cloudflare import CloudflareProvider
from ai.providers.groq import GroqProvider


class AIProviderManager:
    """Route AI operations to the configured provider.

    Providers are selected from configuration without exposing their protocol
    details to classification or extraction services.
    """

    PROVIDERS = {
        "groq": GroqProvider,
        "cloudflare": CloudflareProvider,
    }

    def __init__(self, provider=None, provider_name=None):
        if provider is not None:
            self.provider_instance = provider
            return
        selected = (provider_name or (config.AI_PROVIDERS[0] if config.AI_PROVIDERS else "groq")).lower()
        try:
            self.provider_instance = self.PROVIDERS[selected]()
        except KeyError as exc:
            raise RuntimeError(f"Unsupported AI provider: {selected}") from exc

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
