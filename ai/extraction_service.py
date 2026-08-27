"""Interface boundary for future category-specific AI extraction."""

from __future__ import annotations

from typing import Protocol


class CategoryExtractionService(Protocol):
    """Future service contract for category-specific extraction workers."""

    def process_pending(self, limit=100, channel_username=None, should_stop=None):
        """Extract structured fields for messages that already have an AI category."""

    def process_pending_with_stats(self, limit=100, channel_username=None, should_stop=None):
        """Extract pending category records and return pipeline stats."""
