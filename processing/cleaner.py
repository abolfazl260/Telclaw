"""Deterministic cleaning for newly collected Telegram messages."""


def clean_text(text):
    """Return normalized whitespace while preserving message content."""
    if text is None:
        return ""
    return " ".join(str(text).split())


def is_collectable_text(text, min_length=20):
    """Apply the crawler's current minimum-text rule without side effects."""
    return len(clean_text(text)) >= min_length
