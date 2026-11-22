"""Regression coverage for message state migration during startup."""

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
