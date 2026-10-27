"""Import an administrator's forwarded Telegram post as a normal pending message.

The text supplies an *unverified contact handle*, not a Telegram-verified author.
Keep that provenance separate from the authenticated administrator and the real
forward origin; do not invent a sender ID or a public source message URL.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

from storage import database
from storage.content_duplicate import content_hash

# Deliberately invalid as a public Telegram username: never fabricate t.me links.
ADMIN_FORWARD_PREFIX = "admin-forward:"
SENDER_TYPE = "admin_forward_text"
HANDLE = r"[A-Za-z][A-Za-z0-9_]{4,31}"
MENTION_RE = re.compile(r"(?<![\w@])@(" + HANDLE + r")(?!\w)")
PROFILE_RE = re.compile(
    r"(?<![\w/])(?:https?://)?(?:t\.me|telegram\.me)/(" + HANDLE + r")(?![\w/])",
    re.IGNORECASE,
)
LABEL_RE = re.compile(
    r"(?i)(?:username|user\s*name|telegram(?:\s*id)?|contact|یوزرنیم|آیدی(?:\s*تلگرام)?|شناسه\s*تلگرام)"
    r"\s*[:：\-]\s*@?(" + HANDLE + r")(?!\w)"
)


def word_count(text: str) -> int:
    return len(str(text or "").split())


def extract_text_username(text: str) -> str | None:
    """Return a single unambiguous Telegram username, or None.

    A labeled contact takes precedence over unrelated mentions in the body;
    conflicting labeled handles are refused. Otherwise all distinct @mentions
    and direct Telegram profile links must agree.
    """
    labeled = {m.group(1).lower(): m.group(1) for m in LABEL_RE.finditer(text)}
    if len(labeled) > 1:
        return None
    if labeled:
        return "@" + next(iter(labeled.values()))
    candidates = {}
    for regex in (MENTION_RE, PROFILE_RE):
        for match in regex.finditer(text):
            candidates.setdefault(match.group(1).lower(), match.group(1))
    if len(candidates) != 1:
        return None
    return "@" + next(iter(candidates.values()))


def is_forwarded(message: dict) -> bool:
    """Use Bot API forward metadata, never a textual 'Forwarded' claim."""
    return bool(
        isinstance(message.get("forward_origin"), dict)
        or isinstance(message.get("forward_from"), dict)
        or isinstance(message.get("forward_from_chat"), dict)
        or message.get("forward_sender_name")
    )


def _original_forward_date(message: dict) -> str | None:
    origin = message.get("forward_origin")
    date_value = origin.get("date") if isinstance(origin, dict) else None
    if date_value is None:
        date_value = message.get("forward_date")
    try:
        stamp = int(date_value)
        if stamp <= 0:
            return None
        date = datetime.fromtimestamp(stamp, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if date > datetime.now(timezone.utc) + timedelta(minutes=5):
        return None
    return date.isoformat()


def ingest_admin_forward(message: dict) -> tuple[str, str]:
    """Save accepted forwarded text in the existing processing/classification queue.

    Returns (result_code, optional_message). Never runs AI or publishes directly.
    Caller MUST check the administrator's authenticated private-chat identity.
    """
    if not is_forwarded(message):
        return "not_forwarded", "Only forwarded Telegram messages are accepted."
    text = message.get("text") or message.get("caption") or ""
    if not isinstance(text, str) or word_count(text) <= 10:
        return "too_short", "Forwarded text must contain more than 10 words."
    username = extract_text_username(text)
    if not username:
        return "missing_username", (
            "Add one unambiguous Telegram username in the forwarded text "
            "(for example @example_user or Username: @example_user)."
        )
    date_str = _original_forward_date(message)
    if not date_str:
        return "missing_forward_date", "Telegram did not provide a valid original forward date."
    actor = message.get("from") or {}
    try:
        admin_id = int(actor.get("id"))
        bot_message_id = int(message.get("message_id"))
        if admin_id <= 0 or bot_message_id <= 0:
            raise ValueError("invalid message identity")
    except (ValueError, TypeError, AttributeError):
        return "invalid_message_id", "Invalid Telegram message identity."
    channel = f"{ADMIN_FORWARD_PREFIX}{admin_id}"

    # Existing content_hash fingerprint allows repeat forwards to be idempotent
    # even if a different admin submitted the same exact normalized message.
    fingerprint = content_hash(text)
    with database.get_connection() as conn:
        previous = conn.execute(
            "SELECT 1 FROM messages WHERE channel_username LIKE ? "
            "AND content_hash=? LIMIT 1",
            (ADMIN_FORWARD_PREFIX + "%", fingerprint),
        ).fetchone()
        if previous:
            return "duplicate", "This forwarded content was already imported."

    inserted = database.insert_message(
        channel_username=channel,
        message_id=bot_message_id,
        text=text,
        raw_text=text,
        cleaned_text=None,
        date_str=date_str,
        collection_status="collected",
        processing_status="pending",
        ai_status="waiting",
        pipeline_version="admin-forward-v1",
        channel_name="Admin forwarded submission",
        sender_id=None,
        sender_username=username.lstrip("@"),
        sender_type=SENDER_TYPE,
        sender_is_original=0,
        message_link=None,
        has_media=False,
    )
    if not inserted:
        return "duplicate", "This Telegram message was already imported."
    # The database's existing startup schema supplies content_hash.
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE messages SET content_hash=? "
            "WHERE channel_username=? AND message_id=?",
            (fingerprint, channel, bot_message_id),
        )
        conn.commit()
    return "saved", username
