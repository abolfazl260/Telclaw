"""Select text from persisted Telegram messages for AI workers.

The on-disk `messages.text` column remains a legacy compatibility field.
This helper does not edit message records or change the classification schema.
"""


def select_source_text(record):
    """Pick the first usable string: cleaned, legacy, then original raw text.

    Non-string or whitespace-only values must not prevent fallback to a valid
    lower-priority field. Apart from that malformed/blank-field edge case,
    preserve the workers' existing precedence and leading/trailing trim.
    """
    for field in ("cleaned_text", "text", "raw_text"):
        value = record.get(field)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return ""
