"""Batch AI category classification for cleaned Telclaw messages."""

from __future__ import annotations

from ai.category_schemas import CLASSIFICATION_CATEGORIES


def build_classification_prompt(categories=None):
    categories = tuple(categories or CLASSIFICATION_CATEGORIES)
    valid_categories = ", ".join(f'"{category}"' for category in categories)
    return (
        "Classify each cleaned Telegram marketplace message into exactly ONE category.\n"
        f"Valid categories are exactly: {valid_categories}.\n"
        "The category field MUST contain exactly one of these strings.\n"
        "NEVER combine categories. NEVER use a pipe character (|). NEVER return a list of categories.\n\n"
        "Category guide:\n"
        "- housinglist: rental housing, rooms, roommates, properties, apartments, condos, basements, studios.\n"
        "- transferlist: air-cargo, passenger baggage, luggage space, parcel/package carried by airline passenger, flight-based shipping.\n"
        "- joblist: job offers, hiring, work requests, employment, services offered as work.\n"
        "- none: irrelevant, unclear, spam, news, discussion, or not a marketplace listing.\n\n"
        "Return ONLY JSON with this exact shape:\n"
        '{"classifications":[{"message_id":101,"category":"housinglist"}]}\n'
        "Replace the example category with exactly ONE valid category for each input.\n"
        "You MUST return exactly one classification for EVERY input message_id.\n"
        "Keep every input message_id exactly as provided. Do not omit or merge messages.\n"
        "Do not include explanations."
    )


def validate_classification_result(result, requested_ids, categories=None):
    categories = set(categories or CLASSIFICATION_CATEGORIES)
    requested_ids = [int(message_id) for message_id in requested_ids]
    requested_set = set(requested_ids)
    if not isinstance(result, dict):
        raise ValueError("classification result must be an object")
    items = result.get("classifications")
    if not isinstance(items, list):
        raise ValueError("classification result must contain classifications list")
    parsed = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("classification item must be an object")
        message_id = int(item.get("message_id"))
        category = str(item.get("category") or "").strip().lower()
        if message_id not in requested_set:
            raise ValueError(f"unexpected message_id in classification result: {message_id}")
        if "|" in category:
            raise ValueError(f"classification category must be exactly one value, got pipe-separated value: {category!r}")
        if category not in categories:
            raise ValueError(f"unsupported classification category: {category!r}")
        parsed[message_id] = category
    missing = [message_id for message_id in requested_ids if message_id not in parsed]
    if missing:
        raise ValueError(f"missing classification for message_id(s): {missing}")
    return parsed


def __getattr__(name):
    # Compatibility for callers importing the former concrete classifier.
    if name == "GroqBatchCategoryClassifier":
        from ai.providers.groq import _GroqBatchClassifier
        return _GroqBatchClassifier
    raise AttributeError(name)


__all__ = ["GroqBatchCategoryClassifier", "build_classification_prompt", "validate_classification_result"]
