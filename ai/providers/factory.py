"""AI provider factory and registry."""

from ai.providers.cloudflare import CloudflareProvider
from ai.providers.groq import GroqProvider


PROVIDER_REGISTRY = {
    "groq": GroqProvider,
    "cloudflare": CloudflareProvider,
}


def register_provider(name, provider_class):
    PROVIDER_REGISTRY[name.strip().lower()] = provider_class


def create_provider(name):
    if not name or not str(name).strip():
        raise ValueError("AI provider name is required")
    normalized = str(name).strip().lower()
    provider_class = PROVIDER_REGISTRY.get(normalized)
    if provider_class is None:
        raise ValueError(f"Unknown AI provider: {name}. Supported providers: groq, cloudflare")
    return provider_class()
