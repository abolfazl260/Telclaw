"""Backward-compatible facade for the collection layer.

New code should import from ``collection.crawler`` directly.
"""

from collection.crawler import start_crawler

__all__ = ["start_crawler"]
