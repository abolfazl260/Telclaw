"""Content-based duplicate detection for collected Telegram messages."""

import hashlib
import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_message_text(text):
    """Return a stable, case-insensitive representation for duplicate matching."""
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized.casefold()


def content_hash(text):
    """Return a SHA-256 fingerprint of normalized message text."""
    normalized = normalize_message_text(text)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ensure_duplicate_schema(conn):
    """Add and backfill the content fingerprint without changing existing uniqueness."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "content_hash" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN content_hash TEXT")
    rows = conn.execute(
        "SELECT id, text FROM messages WHERE content_hash IS NULL AND text IS NOT NULL"
    ).fetchall()
    for row in rows:
        fingerprint = content_hash(row[1])
        if fingerprint:
            conn.execute(
                "UPDATE messages SET content_hash=? WHERE id=?",
                (fingerprint, row[0]),
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_sender_content_hash "
        "ON messages(sender_id, content_hash)"
    )
    conn.commit()


def find_duplicate(conn, sender_id, text):
    """Find an existing message with the same sender and normalized textual content."""
    if sender_id is None:
        return None
    fingerprint = content_hash(text)
    if fingerprint is None:
        return None
    return conn.execute(
        """SELECT id, channel_username, message_id
           FROM messages
          WHERE sender_id=? AND content_hash=?
          ORDER BY id ASC
          LIMIT 1""",
        (sender_id, fingerprint),
    ).fetchone()


def store_content_hash(conn, message_row_id, text):
    """Persist the fingerprint for an accepted message."""
    fingerprint = content_hash(text)
    if fingerprint:
        conn.execute(
            "UPDATE messages SET content_hash=? WHERE id=?",
            (fingerprint, message_row_id),
        )
        conn.commit()
    return fingerprint
