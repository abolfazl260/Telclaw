"""AI provider factory and registry."""

from ai.providers.groq import GroqProvider


PROVIDER_REGISTRY = {
    "groq": GroqProvider,
}


def register_provider(name, provider_class):
    PROVIDER_REGISTRY[name] = provider_class


def create_provider(name):
    if not name:
        raise ValueError("AI provider name is required")

    provider_class = PROVIDER_REGISTRY.get(name.lower())
    if provider_class is None:
        raise ValueError(f"Unknown AI provider: {name}")

    return provider_class()
