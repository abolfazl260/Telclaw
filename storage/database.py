"""SQLite persistence for the Telclaw pipeline.

This module owns persistence. It does not know about Telegram, UI, CSV, AI,
or external delivery services.
"""

import sqlite3
from pathlib import Path

import config


MESSAGE_COLUMNS = {
    "raw_text": "TEXT",
    "cleaned_text": "TEXT",
    "processing_status": "TEXT NOT NULL DEFAULT 'collected'",
    "pipeline_version": "TEXT",
    "cleaned_at": "TEXT",
    "channel_id": "INTEGER",
    "channel_name": "TEXT",
    "sender_id": "INTEGER",
    "sender_username": "TEXT",
    "sender_type": "TEXT",
    "has_media": "INTEGER NOT NULL DEFAULT 0",
    "media_type": "TEXT",
    "file_unique_id": "TEXT",
}


def get_connection():
    db_path = Path(config.DB_NAME)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_messages_table(cursor):
    cursor.execute("PRAGMA table_info(messages)")
    existing = {row[1] for row in cursor.fetchall()}
    for column, definition in MESSAGE_COLUMNS.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")


def initialize_db():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                text TEXT,
                raw_text TEXT,
                cleaned_text TEXT,
                date TEXT NOT NULL,
                media_path TEXT,
                processing_status TEXT NOT NULL DEFAULT 'collected',
                pipeline_version TEXT,
                cleaned_at TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                sender_id INTEGER,
                sender_username TEXT,
                sender_type TEXT,
                has_media INTEGER NOT NULL DEFAULT 0,
                media_type TEXT,
                file_unique_id TEXT,
                UNIQUE(channel_username, message_id)
            )
            """
        )
        _migrate_messages_table(cursor)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_channel_date "
            "ON messages(channel_username, date)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_processing_status "
            "ON messages(processing_status)"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS crawler_settings (
                channel_username TEXT PRIMARY KEY,
                target_date TEXT NOT NULL,
                last_crawled_date TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def insert_message(channel_username, message_id, text, date_str, *,
                   raw_text=None, cleaned_text=None, processing_status="collected",
                   pipeline_version=None, cleaned_at=None, channel_id=None,
                   channel_name=None, sender_id=None, sender_username=None,
                   sender_type=None, has_media=False, media_type=None,
                   file_unique_id=None, media_path=None):
    """Insert a new collected record; return False for an existing message."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO messages (
                channel_username, message_id, text, raw_text, cleaned_text, date,
                media_path, processing_status, pipeline_version, cleaned_at,
                channel_id, channel_name, sender_id, sender_username, sender_type,
                has_media, media_type, file_unique_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_username, message_id, text, raw_text, cleaned_text, date_str,
                media_path, processing_status, pipeline_version, cleaned_at,
                channel_id, channel_name, sender_id, sender_username, sender_type,
                int(bool(has_media)), media_type,
                str(file_unique_id) if file_unique_id is not None else None,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def set_channel_target_date(channel_username, target_date_str):
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO crawler_settings (channel_username, target_date)
            VALUES (?, ?)
            ON CONFLICT(channel_username)
            DO UPDATE SET target_date = excluded.target_date
            """,
            (channel_username, target_date_str),
        )
        conn.commit()
    finally:
        conn.close()


def get_channel_target_date(channel_username):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT target_date FROM crawler_settings WHERE channel_username = ?",
            (channel_username,),
        ).fetchone()
        return row["target_date"] if row else None
    finally:
        conn.close()
