"""Provider contract used by Telclaw AI business services."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AIProvider(ABC):
    """Provider-independent interface for classification and extraction."""

    name = "base"

    @abstractmethod
    def classify_batch(self, messages):
        """Return a mapping of message IDs to categories."""
        raise NotImplementedError

    @abstractmethod
    def extract(self, text, category):
        """Extract validated data for the authoritative category."""
        raise NotImplementedError
