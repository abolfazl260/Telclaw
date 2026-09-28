"""Backward-compatible name for Groq's internal multi-key routing."""

from ai.providers.groq import GroqProvider


class GroqProviderFailover(GroqProvider):
    """Legacy adapter; application services use :class:`AIProviderManager`."""

    def extract(self, text, category=None):
        if category is None:
            return self._call_with_rotation(
                lambda index, value: self.providers[index].extract(value), text
            )
        return super().extract(text, category)


__all__ = ["GroqProviderFailover"]
