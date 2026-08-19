"""Normalization of Telegram metadata into stable application values."""


def normalize_channel_username(value):
    if value is None:
        return ""
    return str(value).strip().lstrip("@").lower()


def normalize_date(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
