"""SQLite persistence for the Telclaw pipeline."""

import json
import sqlite3
from pathlib import Path

import config


MESSAGE_COLUMNS = {
    "raw_text": "TEXT",
    "cleaned_text": "TEXT",
    "processing_status": "TEXT NOT NULL DEFAULT 'pending'",
    "collection_status": "TEXT NOT NULL DEFAULT 'collected'",
    "ai_status": "TEXT NOT NULL DEFAULT 'waiting'",
    "pipeline_version": "TEXT",
    "cleaned_at": "TEXT",
    "ai_category": "TEXT",
    "ai_processed_at": "TEXT",
    "ai_error": "TEXT",
    "channel_id": "INTEGER",
    "channel_name": "TEXT",
    "sender_id": "INTEGER",
    "sender_username": "TEXT",
    "sender_type": "TEXT",
    "has_media": "INTEGER NOT NULL DEFAULT 0",
    "media_type": "TEXT",
    "file_unique_id": "TEXT",
    "message_link": "TEXT",
    "media_reference": "TEXT",
    "advertio_status": "TEXT NOT NULL DEFAULT 'waiting'",
    "advertio_lead_id": "TEXT",
    "advertio_error": "TEXT",
    "advertio_processed_at": "TEXT",
}

CATEGORY_TABLES = {
    "housinglist": {
        "property_type": "TEXT", "listing_type": "TEXT", "title": "TEXT",
        "description": "TEXT", "location": "TEXT", "country_code": "TEXT",
        "province": "TEXT", "city": "TEXT", "neighborhood": "TEXT",
        "price": "REAL", "currency": "TEXT", "rent_period": "TEXT",
        "bedrooms": "INTEGER", "bathrooms": "REAL", "area": "REAL",
        "area_unit": "TEXT", "furnished": "INTEGER", "availability": "TEXT",
        "property_condition": "TEXT", "contact": "TEXT", "features": "TEXT",
    },
    "transferlist": {
        "vehicle_type": "TEXT", "brand": "TEXT", "model": "TEXT", "trim": "TEXT",
        "year": "INTEGER", "mileage": "REAL", "mileage_unit": "TEXT",
        "price": "REAL", "currency": "TEXT", "location": "TEXT",
        "transmission": "TEXT", "fuel_type": "TEXT", "condition": "TEXT",
        "engine": "TEXT", "color": "TEXT", "contact": "TEXT", "features": "TEXT",
    },
    "joblist": {
        "job_title": "TEXT", "company": "TEXT", "location": "TEXT",
        "employment_type": "TEXT", "salary": "REAL", "salary_currency": "TEXT",
        "salary_period": "TEXT", "experience": "TEXT", "education": "TEXT",
        "skills": "TEXT", "remote": "INTEGER", "job_type": "TEXT",
        "description": "TEXT", "application_method": "TEXT", "contact": "TEXT",
    },
}


def get_connection():
    db_path = Path(config.DB_NAME)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_messages_table(cursor):
    cursor.execute("PRAGMA table_info(messages)")
    existing = {row[1] for row in cursor.fetchall()}
    for column, definition in MESSAGE_COLUMNS.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
    cursor.execute("UPDATE messages SET collection_status = COALESCE(collection_status, 'collected')")
    cursor.execute("""
        UPDATE messages SET processing_status = CASE processing_status
            WHEN 'collected' THEN 'pending'
            WHEN 'processed' THEN 'processed'
            WHEN 'processing_failed' THEN 'failed'
            WHEN 'ai_processed' THEN 'processed'
            WHEN 'ai_failed' THEN 'processed'
            ELSE processing_status END
    """)
    cursor.execute("""
        UPDATE messages SET ai_status = CASE
            WHEN processing_status = 'processed' AND ai_category IS NOT NULL THEN 'processed'
            WHEN ai_processed_at IS NOT NULL AND ai_error IS NOT NULL THEN 'failed'
            WHEN ai_processed_at IS NOT NULL THEN 'processed'
            WHEN processing_status = 'processed' THEN 'pending'
            ELSE COALESCE(ai_status, 'waiting') END
    """)


def _create_category_tables(cursor):
    for table, fields in CATEGORY_TABLES.items():
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        exists = cursor.fetchone() is not None
        if not exists:
            columns = [
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "processed_message_id INTEGER NOT NULL UNIQUE",
                *[f"{name} {definition}" for name, definition in fields.items()],
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE",
            ]
            cursor.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
        else:
            cursor.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cursor.fetchall()}
            for name, definition in fields.items():
                if name not in existing:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_processed_message ON {table}(processed_message_id)")


def initialize_db():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                text TEXT,
                raw_text TEXT,
                cleaned_text TEXT,
                date TEXT NOT NULL,
                media_path TEXT,
                message_link TEXT,
                media_reference TEXT,
                collection_status TEXT NOT NULL DEFAULT 'collected',
                processing_status TEXT NOT NULL DEFAULT 'pending',
                ai_status TEXT NOT NULL DEFAULT 'waiting',
                pipeline_version TEXT,
                cleaned_at TEXT,
                ai_category TEXT,
                ai_processed_at TEXT,
                ai_error TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                sender_id INTEGER,
                sender_username TEXT,
                sender_type TEXT,
                has_media INTEGER NOT NULL DEFAULT 0,
                media_type TEXT,
                file_unique_id TEXT,
                advertio_status TEXT NOT NULL DEFAULT 'waiting',
                advertio_lead_id TEXT,
                advertio_error TEXT,
                advertio_processed_at TEXT,
                UNIQUE(channel_username, message_id)
            )
        """)
        _migrate_messages_table(cursor)
        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)",
            "CREATE INDEX IF NOT EXISTS idx_messages_channel_date ON messages(channel_username, date)",
            "CREATE INDEX IF NOT EXISTS idx_messages_collection_status ON messages(collection_status)",
            "CREATE INDEX IF NOT EXISTS idx_messages_processing_status ON messages(processing_status)",
            "CREATE INDEX IF NOT EXISTS idx_messages_ai_status ON messages(ai_status)",
            "CREATE INDEX IF NOT EXISTS idx_messages_media_type ON messages(media_type)",
            "CREATE INDEX IF NOT EXISTS idx_messages_ai_category ON messages(ai_category)",
            "CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id)",
            "CREATE INDEX IF NOT EXISTS idx_messages_advertio_status ON messages(advertio_status)",
        ):
            cursor.execute(sql)
        cursor.execute("CREATE TABLE IF NOT EXISTS crawler_settings (channel_username TEXT PRIMARY KEY, target_date TEXT NOT NULL, last_crawled_date TEXT)")
        _create_category_tables(cursor)
        conn.commit()
    finally:
        conn.close()


def insert_message(channel_username, message_id, text, date_str, *, raw_text=None, cleaned_text=None,
                   collection_status="collected", processing_status="pending", ai_status="waiting",
                   pipeline_version=None, cleaned_at=None, channel_id=None, channel_name=None,
                   sender_id=None, sender_username=None, sender_type=None, has_media=False,
                   media_type=None, file_unique_id=None, media_path=None, message_link=None,
                   media_reference=None):
    conn = get_connection()
    try:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO messages (
                channel_username, message_id, text, raw_text, cleaned_text, date,
                media_path, message_link, media_reference, collection_status,
                processing_status, ai_status, pipeline_version, cleaned_at, channel_id,
                channel_name, sender_id, sender_username, sender_type, has_media,
                media_type, file_unique_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (channel_username, message_id, text, raw_text, cleaned_text, date_str,
              media_path, message_link, media_reference, collection_status,
              processing_status, ai_status, pipeline_version, cleaned_at, channel_id,
              channel_name, sender_id, sender_username, sender_type, int(bool(has_media)),
              media_type, str(file_unique_id) if file_unique_id is not None else None))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _get_messages(where_sql, params, limit, channel_username):
    conn = get_connection()
    try:
        sql = f"SELECT * FROM messages WHERE {where_sql}"
        values = list(params)
        if channel_username:
            sql += " AND channel_username = ?"
            values.append(channel_username)
        sql += " ORDER BY id LIMIT ?"
        values.append(int(limit))
        return [dict(row) for row in conn.execute(sql, values).fetchall()]
    finally:
        conn.close()


def get_messages_by_status(status, limit=500, channel_username=None):
    return _get_messages("processing_status = ?", [status], limit, channel_username)


def get_processing_pending_messages(limit=500, channel_username=None):
    return _get_messages("collection_status = 'collected' AND processing_status = 'pending'", [], limit, channel_username)


def get_ai_pending_messages(limit=100, channel_username=None):
    return _get_messages("processing_status = 'processed' AND ai_status = 'pending'", [], limit, channel_username)


def get_previous_messages_by_sender(sender_id, before_id):
    if sender_id is None:
        return []
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT id, message_id, channel_username, sender_id, raw_text, text
            FROM messages
            WHERE sender_id = ? AND id < ? AND COALESCE(raw_text, text, '') <> ''
            ORDER BY id
        """, (sender_id, before_id)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_message(message_id, channel_username, **fields):
    allowed = {
        "cleaned_text", "text", "collection_status", "processing_status", "ai_status",
        "pipeline_version", "cleaned_at", "ai_category", "ai_processed_at", "ai_error",
        "advertio_status", "advertio_lead_id", "advertio_error", "advertio_processed_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return False
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [channel_username, message_id]
    conn = get_connection()
    try:
        cursor = conn.execute(f"UPDATE messages SET {assignments} WHERE channel_username = ? AND message_id = ?", values)
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_message(message_id, channel_username):
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM messages WHERE channel_username = ? AND message_id = ?", (channel_username, message_id))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_processed_message(message_id, channel_username, **fields):
    return update_message(message_id, channel_username, **fields)


def save_category_record(processed_message_id, category, data):
    if category not in CATEGORY_TABLES:
        raise ValueError(f"Unsupported category: {category}")
    fields = CATEGORY_TABLES[category]
    columns = ["processed_message_id"] + list(fields)
    values = [processed_message_id]
    for field in fields:
        value = data.get(field)
        if field in {"features", "skills"} and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        if field in {"furnished", "remote"} and value is not None:
            value = int(bool(value))
        values.append(value)
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{field}=excluded.{field}" for field in fields)
    conn = get_connection()
    try:
        conn.execute(f"INSERT INTO {category} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT(processed_message_id) DO UPDATE SET {assignments}", values)
        conn.commit()
    finally:
        conn.close()


def get_category_record(processed_message_id, category):
    if category not in CATEGORY_TABLES:
        raise ValueError(f"Unsupported category: {category}")
    conn = get_connection()
    try:
        row = conn.execute(f"SELECT * FROM {category} WHERE processed_message_id = ?", (processed_message_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_channel_target_date(channel_username, target_date_str):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO crawler_settings (channel_username, target_date) VALUES (?, ?) ON CONFLICT(channel_username) DO UPDATE SET target_date = excluded.target_date", (channel_username, target_date_str))
        conn.commit()
    finally:
        conn.close()


def get_channel_target_date(channel_username):
    conn = get_connection()
    try:
        row = conn.execute("SELECT target_date FROM crawler_settings WHERE channel_username = ?", (channel_username,)).fetchone()
        return row["target_date"] if row else None
    finally:
        conn.close()
