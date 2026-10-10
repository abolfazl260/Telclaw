"""Regression coverage for startup AI status migration.

initialize_db() runs on every application restart.  Explicit AI queue
outcomes must not be inferred from ai_processed_at/ai_error again.
"""

from datetime import datetime, timezone

import pytest

import config
from storage import database
from storage.message_repository import MessageRepository


@pytest.fixture
def ai_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "ai-migration.sqlite3"))
    database.initialize_db()
    return MessageRepository()


def _ai_record(message_id):
    with database.get_connection() as conn:
        return dict(conn.execute(
            "SELECT * FROM messages WHERE channel_username='ai_migration' AND message_id=?",
            (message_id,),
        ).fetchone())


def _eligible_record(message_id):
    database.insert_message(
        "ai_migration", message_id, f"sample text {message_id}",
        "2026-10-10", processing_status="processed",
    )
    database.update_message(
        message_id, "ai_migration",
        classification_status="processed",
        classification_category="joblist",
        ai_category="joblist",
    )


def test_failed_extraction_does_not_become_processed_on_restart(ai_db):
    _eligible_record(101)
    now = datetime.now(timezone.utc).isoformat()
    # This is the real service behavior: AI failures persist with ai_error=NULL.
    ai_db.mark_ai_result(101, "ai_migration", success=False, ai_processed_at=now)
    before = _ai_record(101)
    assert before["ai_status"] == "failed"
    assert before["ai_error"] is None

    for _ in range(3):
        database.initialize_db()
        row = _ai_record(101)
        assert row["ai_status"] == "failed"
        assert row["ai_error"] is None
        assert row["ai_processed_at"] == now

    assert database.get_pipeline_status()["ai_failed"] == 1
    assert not ai_db.get_ai_pending(limit=10)


@pytest.mark.parametrize(
    ("status", "processed_at", "error"),
    [
        ("failed", None, None),
        ("failed", "2026-10-09T12:00:00+00:00", "provider unavailable"),
        ("skipped", "2026-10-09T12:00:00+00:00", "skipped:no_text"),
        ("processed", "2026-10-09T12:00:00+00:00", "stale metadata"),
        ("pending", "2026-10-09T12:00:00+00:00", None),
        ("processing", "2026-10-09T12:00:00+00:00", None),
    ],
)
def test_explicit_ai_status_is_authoritative_on_startup(
    ai_db, status, processed_at, error
):
    _eligible_record(102)
    started = datetime.now(timezone.utc).isoformat() if status == "processing" else None
    database.update_message(
        102, "ai_migration",
        ai_status=status, ai_processed_at=processed_at,
        ai_error=error, ai_started_at=started,
    )

    database.initialize_db()
    database.initialize_db()
    row = _ai_record(102)
    assert row["ai_status"] == status
    assert row["ai_processed_at"] == processed_at
    assert row["ai_error"] == error
    if status == "processing":
        assert row["ai_started_at"] == started


@pytest.mark.parametrize(
    ("processed_at", "error", "expected"),
    [
        (None, None, "pending"),
        ("2026-10-09T12:00:00+00:00", None, "processed"),
        ("2026-10-09T12:00:00+00:00", "provider error", "failed"),
        ("2026-10-09T12:00:00+00:00", "skipped:no_text", "skipped"),
    ],
)
def test_only_legacy_waiting_ai_status_is_inferred(
    ai_db, processed_at, error, expected
):
    _eligible_record(103)
    database.update_message(
        103, "ai_migration", ai_status="waiting",
        ai_processed_at=processed_at, ai_error=error,
    )

    database.initialize_db()
    assert _ai_record(103)["ai_status"] == expected
    database.initialize_db()
    assert _ai_record(103)["ai_status"] == expected


def test_unclassified_waiting_record_is_not_misclassified(ai_db):
    database.insert_message(
        "ai_migration", 104, "not classified", "2026-10-10",
        processing_status="processed",
    )
    database.initialize_db()
    assert _ai_record(104)["ai_status"] == "waiting"
