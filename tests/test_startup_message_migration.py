"""Regression coverage for message state migration during startup."""

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone

import pytest

import config
from storage import database
from storage.message_repository import MessageRepository


@pytest.fixture
def message_db(tmp_path, monkeypatch):
    path = tmp_path / "messages.sqlite3"
    monkeypatch.setattr(config, "DB_NAME", str(path))
    monkeypatch.setattr(config, "QUEUE_PROCESSING_TIMEOUT_SECONDS", 3600)
    database.initialize_db()
    return path


def _messages():
    with closing(database.get_connection()) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM messages ORDER BY id")]


def _create_update_audit(conn):
    conn.executescript("""
        CREATE TABLE message_updates(message_id INTEGER NOT NULL);
        CREATE TRIGGER audit_message_update AFTER UPDATE ON messages
        BEGIN
            INSERT INTO message_updates VALUES (NEW.message_id);
        END;
    """)


def _message_updates():
    with closing(database.get_connection()) as conn:
        return Counter(row[0] for row in conn.execute("SELECT message_id FROM message_updates"))


def test_restart_preserves_failed_classification_and_manual_retry(message_db):
    repository = MessageRepository()
    timestamp = datetime.now(timezone.utc).isoformat()
    database.insert_message(
        "fixture", 1, "test message", timestamp, processing_status="processed",
    )
    database.update_message(1, "fixture", classification_category="joblist")
    repository.mark_classification_result(
        1, "fixture", success=False, error="provider unavailable",
        processed_at=timestamp, attempts=config.AI_CLASSIFICATION_MAX_RETRIES,
    )
    failed = _messages()
    assert failed[0]["classification_status"] == "failed"
    assert failed[0]["ai_status"] == "waiting"

    for _ in range(2):
        database.initialize_db()
        assert _messages() == failed
        assert not repository.get_ai_pending()

    assert database.retry_failed_classifications() == 1
    pending = _messages()
    assert pending[0]["classification_status"] == "pending"
    database.initialize_db()
    assert _messages() == pending
    assert [row["message_id"] for row in repository.get_classification_pending()] == [1]
    assert not repository.get_ai_pending()


def test_current_schema_restarts_do_not_update_unchanged_messages(message_db):
    timestamp = datetime.now(timezone.utc).isoformat()
    with closing(database.get_connection()) as conn, conn:
        conn.executemany("""
            INSERT INTO messages(
                channel_username, message_id, date, processing_status,
                processing_started_at, classification_status, classification_category,
                classification_started_at, ai_status, ai_started_at,
                classification_error, classification_processed_at, ai_error, ai_processed_at
            ) VALUES ('fixture', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (1, timestamp, "pending", None, "waiting", None, None, "waiting", None,
             None, None, None, None),
            (2, timestamp, "processing", timestamp, "waiting", None, None, "waiting", None,
             None, None, None, None),
            (3, timestamp, "processed", None, "processing", "joblist", timestamp, "waiting", None,
             None, None, None, None),
            (4, timestamp, "processed", None, "processed", "joblist", None, "processing", timestamp,
             None, timestamp, None, None),
            (5, timestamp, "processed", None, "processed", "joblist", None, "failed", None,
             None, timestamp, "rate_limit", timestamp),
            (6, timestamp, "processed", None, "processed", "none", None, "skipped", None,
             None, timestamp, "skipped:no_text", timestamp),
            (7, timestamp, "processed", None, "processed", "joblist", None, "processed", None,
             None, timestamp, None, timestamp),
            (8, timestamp, "failed", None, "waiting", None, None, "waiting", None,
             None, None, None, None),
            (9, timestamp, "processed", None, "failed", "joblist", None, "waiting", None,
             "invalid_provider_output", timestamp, None, None),
            (10, timestamp, "processed", None, "pending", "joblist", None, "pending", None,
             None, None, None, None),
        ])
        _create_update_audit(conn)

    before = _messages()
    for _ in range(2):
        database.initialize_db()
        assert _messages() == before
        assert not _message_updates()


def test_legacy_schema_backfills_only_values_that_change(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "legacy.sqlite3"))
    timestamp = "2026-08-21T10:30:00+00:00"
    with closing(database.get_connection()) as conn, conn:
        conn.executescript("""
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT NOT NULL, message_id INTEGER NOT NULL,
                text TEXT, date TEXT NOT NULL, collection_status TEXT, processing_status TEXT,
                classification_status TEXT, classification_category TEXT, ai_status TEXT,
                ai_processed_at TEXT, ai_error TEXT, processing_started_at TEXT,
                classification_started_at TEXT, ai_started_at TEXT,
                UNIQUE(channel_username, message_id)
            );
        """)
        conn.executemany("""
            INSERT INTO messages(
                channel_username, message_id, text, date, collection_status, processing_status,
                classification_status, classification_category, ai_status, ai_processed_at,
                ai_error, processing_started_at, classification_started_at, ai_started_at
            ) VALUES ('fixture', ?, 'original text', '2026-08-21', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (1, None, "collected", None, None, None, None, None, timestamp, timestamp, timestamp),
            (2, "collected", "processing_failed", "waiting", None, "waiting", None, None, None, None, None),
            (3, "collected", "ai_processed", "waiting", "transferlist", "waiting", timestamp, None, None, None, None),
            (4, "collected", "ai_failed", None, "housinglist", None, timestamp, "provider error", None, None, None),
            (5, "collected", "processed", "waiting", None, "legacy_unknown", None, None, None, None, None),
            (6, "collected", "processed", "legacy_unknown", "joblist", "waiting", None, None, None, None, None),
            (7, "collected", None, "waiting", None, "waiting", None, None, timestamp, None, None),
            (8, "collected", "processed", "processed", "joblist", "legacy_unknown", None, None, None, None, None),
        ])
        _create_update_audit(conn)

    database.initialize_db()
    rows = _messages()
    assert [
        (row["collection_status"], row["processing_status"],
         row["classification_status"], row["ai_status"])
        for row in rows
    ] == [
        ("collected", "pending", "waiting", "waiting"),
        ("collected", "failed", "waiting", "waiting"),
        ("collected", "processed", "processed", "processed"),
        ("collected", "processed", "processed", "failed"),
        ("collected", "processed", "pending", "legacy_unknown"),
        ("collected", "processed", "legacy_unknown", "waiting"),
        ("collected", None, "waiting", "waiting"),
        ("collected", "processed", "processed", "pending"),
    ]
    assert _message_updates() == {1: 7, 2: 1, 3: 3, 4: 3, 5: 1, 7: 1, 8: 1}
    assert all(row["text"] == "original text" for row in rows)
    assert all(row[name] is None for row in rows for name in (
        "processing_started_at", "classification_started_at", "ai_started_at",
    ))
    assert rows[2]["ai_processed_at"] == rows[3]["ai_processed_at"] == timestamp
    assert rows[3]["ai_error"] == "provider error"

    updates = _message_updates()
    database.initialize_db()
    assert _messages() == rows
    assert _message_updates() == updates
