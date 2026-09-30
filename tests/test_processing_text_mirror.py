"""Regression tests for both processing services' text-column persistence contract.

Uses temporary SQLite databases only. All pipeline stages are local and make no
Telegram, AI, or publishing requests.
"""

import sqlite3

import pytest

import config
from processing.cleaner import clean_text
from processing.contracts import ProcessingRecord
from processing.processing_service import ProcessingService as CoreProcessingService
from processing.stages import CleanTextStage, Pipeline
from services.processing_service import ProcessingService as AppProcessingService
from storage import database


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "text-processing.sqlite3"))
    database.initialize_db()
    return tmp_path


def insert_pending(*, raw_text, text, message_id=10):
    assert database.insert_message(
        "source", message_id, text, "2026-09-29",
        raw_text=raw_text,
        cleaned_text=None,
        processing_status="pending",
        sender_id=None,  # Avoid invoking unrelated duplicate-removal policy.
    )


def persisted(message_id=10):
    conn = database.get_connection()
    try:
        return dict(conn.execute(
            "SELECT raw_text,text,cleaned_text,processing_status,"
            "classification_status,ai_status,pipeline_version,cleaned_at "
            "FROM messages WHERE message_id=?", (message_id,)
        ).fetchone())
    finally:
        conn.close()


def run_worker(worker_name, *, stage=None):
    if worker_name == "core":
        pipeline = Pipeline([CleanTextStage(), stage]) if stage else None
        return CoreProcessingService(pipeline=pipeline).process_pending()
    stages = [CleanTextStage(), stage] if stage else None
    return AppProcessingService(stages=stages).process_pending_with_stats()


@pytest.mark.parametrize("worker_name", ["core", "app"])
@pytest.mark.parametrize(
    "raw_text",
    [
        "  سلام   دنیا 🧳 \n ارسال   بار \t فردا  ",
        "  Emoji 😀  URL https://example.com/?a=1&b=2  +1-555-0181!  ",
        " \n\t ",
        "",
    ],
)
def test_successful_cleaning_mirrors_text_and_preserves_original(
    local_db, worker_name, raw_text
):
    insert_pending(raw_text=raw_text, text=raw_text)
    result = run_worker(worker_name)

    assert result["processed"] == 1
    assert result["failed"] == 0
    row = persisted()
    assert row["raw_text"] == raw_text
    assert row["text"] == clean_text(raw_text)
    assert row["cleaned_text"] == clean_text(raw_text)
    assert row["processing_status"] == "processed"
    assert row["pipeline_version"] == "processing-v1"
    assert row["cleaned_at"]
    if worker_name == "app":
        assert row["classification_status"] == "pending"
        assert row["ai_status"] == "waiting"

    repeat = run_worker(worker_name)
    assert repeat["processed"] == 0
    assert persisted() == row


@pytest.mark.parametrize("worker_name", ["core", "app"])
@pytest.mark.parametrize(
    "raw_text,old_text,expected",
    [
        (None, "Legacy  محتوا \n unchanged?", "Legacy محتوا unchanged?"),
        ("", "Text-only  message with spaces", "Text-only message with spaces"),
        (None, None, ""),
    ],
)
def test_legacy_null_and_empty_raw_fallback_preserves_original_field(
    local_db, worker_name, raw_text, old_text, expected
):
    insert_pending(raw_text=raw_text, text=old_text)
    stats = run_worker(worker_name)
    assert stats["processed"] == 1
    row = persisted()
    assert row["raw_text"] == raw_text
    assert row["text"] == row["cleaned_text"] == expected


@pytest.mark.parametrize("worker_name", ["core", "app"])
def test_later_pipeline_stage_cannot_make_persisted_text_diverge(
    local_db, worker_name
):
    class DivergentWorkingText:
        def process(self, record):
            record.data["text"] = "working copy modified by later stage"
            return record

    source = "Original \n   stable cleaned text"
    insert_pending(raw_text=source, text=source)
    result = run_worker(worker_name, stage=DivergentWorkingText())

    assert result["processed"] == 1
    row = persisted()
    assert row["raw_text"] == source
    assert row["text"] == row["cleaned_text"] == clean_text(source)


@pytest.mark.parametrize("worker_name", ["core", "app"])
def test_processing_stage_error_preserves_text_columns_and_states(
    local_db, worker_name
):
    class BrokenStage:
        def process(self, record):
            raise RuntimeError("intentional processing failure")

    source = " Original  noncanonical \n text "
    insert_pending(raw_text=source, text=source)
    result = run_worker(worker_name, stage=BrokenStage())

    assert result["processed"] == 0
    assert result["failed"] == 1
    row = persisted()
    assert row["raw_text"] == source
    assert row["text"] == source
    assert row["cleaned_text"] is None
    assert row["processing_status"] == ("pending" if worker_name == "core" else "failed")
    assert row["cleaned_at"] is None


@pytest.mark.parametrize("worker_name", ["core", "app"])
def test_sql_failure_rolls_back_both_mirrored_columns(
    local_db, worker_name
):
    source = "  original   source kept on failed SQL update  "
    insert_pending(raw_text=source, text=source)
    conn = database.get_connection()
    try:
        conn.execute(
            """CREATE TRIGGER reject_cleaned_write
            BEFORE UPDATE OF cleaned_text ON messages
            WHEN NEW.cleaned_text IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"""
        )
        conn.commit()
    finally:
        conn.close()

    result = run_worker(worker_name)
    assert result["processed"] == 0
    assert result["failed"] == 1
    row = persisted()
    assert row["raw_text"] == source
    assert row["text"] == source
    assert row["cleaned_text"] is None
    assert row["processing_status"] == ("pending" if worker_name == "core" else "failed")


@pytest.mark.parametrize("raw_text,old_text,expected", [
    (None, "Legacy\n text  only", "Legacy text only"),
    ("", "Old  fallback", "Old fallback"),
    ("\n\t", "not selected", ""),
])
def test_clean_text_stage_sets_both_derived_keys_without_changing_source(
    raw_text, old_text, expected
):
    record = ProcessingRecord(data={
        "raw_text": raw_text, "text": old_text,
    })
    result = CleanTextStage().process(record).data
    assert result["raw_text"] == raw_text
    assert result["cleaned_text"] == result["text"] == expected
