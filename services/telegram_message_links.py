"""Canonical, validated links to source Telegram channel messages.

An ID alone is not sufficient for a Telegram link. Public links require a
realistic channel username; private links require a numeric channel identifier.
Never link to pseudo-sources such as admin-forward imports.
"""
from __future__ import annotations

import re

_MESSAGE_ID = re.compile(r"[1-9][0-9]*\Z")
_PUBLIC_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
_PRIVATE_CHAT_ID = re.compile(r"-100([1-9][0-9]*)\Z")
_NUMERIC_ID = re.compile(r"[1-9][0-9]*\Z")
_SAVED_LINK = re.compile(
    r"https://t\.me/(?:[A-Za-z][A-Za-z0-9_]{4,31}|c/[1-9][0-9]*)/"
    r"([1-9][0-9]*)(?:\?single)?\Z"
)


def build_telegram_message_link(channel_username, message_id, *, channel_id=None,
                                stored_link=None):
    """Prefer a valid stored permalink, otherwise derive one from source IDs.

    The resulting link is only a *possible* deep link: access to private posts
    still depends on membership, and a deleted post will not be available.
    """
    if isinstance(message_id, bool):
        return None
    message_id = str(message_id or "").strip()
    if not _MESSAGE_ID.fullmatch(message_id):
        return None

    if isinstance(stored_link, str):
        stored_link = stored_link.strip()
        match = _SAVED_LINK.fullmatch(stored_link)
        if match and match.group(1) == message_id:
            return stored_link

    username = str(channel_username or "").strip().lstrip("@")
    if _PUBLIC_USERNAME.fullmatch(username):
        return f"https://t.me/{username}/{message_id}"

    private = _PRIVATE_CHAT_ID.fullmatch(username)
    if private:
        return f"https://t.me/c/{private.group(1)}/{message_id}"
    if _NUMERIC_ID.fullmatch(username):
        return f"https://t.me/c/{username}/{message_id}"

    # A true Telegram channel ID can be passed separately even when the source
    # has no public username. Do not turn synthetic admin-forward IDs into links.
    if username.startswith("admin-forward:") or username.startswith("admin_forward:"):
        return None
    if channel_id is not None and not isinstance(channel_id, bool):
        raw_id = str(channel_id).strip()
        private = _PRIVATE_CHAT_ID.fullmatch(raw_id)
        numeric = private.group(1) if private else raw_id
        if _NUMERIC_ID.fullmatch(numeric):
            return f"https://t.me/c/{numeric}/{message_id}"
    return None
