"""Provider-agnostic entry point for Telclaw AI operations."""

from __future__ import annotations

import os

from ai.providers.factory import create_provider


class AIProviderManager:
    """Route AI operations without provider-specific dependencies.

    Provider selection belongs here. Business services only depend on this
    manager and never need to know provider names or implementations.
    """

    def __init__(self, provider=None, provider_name=None):
        if provider is not None:
            self.provider_instance = provider
        else:
            selected_provider = provider_name or os.getenv(
                "AI_PROVIDER_1",
                "groq",
            )
            self.provider_instance = create_provider(selected_provider)

    @property
    def provider(self):
        return getattr(
            self.provider_instance,
            "provider",
            getattr(self.provider_instance, "name", "unknown"),
        )

    @property
    def model(self):
        return getattr(self.provider_instance, "model", None)

    def classify(self, text):
        return self.provider_instance.classify(text)

    def classify_batch(self, messages):
        return self.provider_instance.classify_batch(messages)

    def extract(self, text, category):
        return self.provider_instance.extract(text, category)

    def health_check(self):
        if hasattr(self.provider_instance, "health_check"):
            return self.provider_instance.health_check()
        return {"status": "unknown", "provider": self.provider}

    def get_status(self):
        if hasattr(self.provider_instance, "get_status"):
            return self.provider_instance.get_status()
        return {"provider": self.provider, "model": self.model}


__all__ = ["AIProviderManager"]
