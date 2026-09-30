"""Contract tests for AI classification/extraction source-text selection.

No network requests: use a temporary SQLite database and a recording provider.
"""

from __future__ import annotations

import pytest

import config
from ai.ai_service import AIProcessingService
from ai.classification_service import CategoryClassificationService
from ai.text_source import select_source_text
from processing.classifier import ClassifierStage
from processing.contracts import ProcessingRecord
from storage import database
from storage.message_repository import MessageRepository


@pytest.mark.parametrize(
    "values,expected",
    [
        (
            {"cleaned_text": "  Cleaned   ✓  ", "text": "legacy", "raw_text": "original"},
            "Cleaned   ✓",
        ),
        ({"cleaned_text": None, "text": "  Legacy   محتوا ", "raw_text": "raw"}, "Legacy   محتوا"),
        ({"cleaned_text": "", "text": "legacy", "raw_text": "raw"}, "legacy"),
        ({"cleaned_text": None, "text": None, "raw_text": "  Raw   🧳 \n "}, "Raw   🧳"),
        ({"cleaned_text": None, "text": "", "raw_text": "original"}, "original"),
        ({"cleaned_text": "   \t ", "text": "  valid legacy ", "raw_text": "raw"}, "valid legacy"),
        ({"cleaned_text": "\n", "text": "  ", "raw_text": " Valid raw  "}, "Valid raw"),
        ({"cleaned_text": 7, "text": {"malformed": "value"}, "raw_text": "raw fallback"}, "raw fallback"),
        ({"cleaned_text": 42, "text": ["invalid"], "raw_text": None}, ""),
        ({"cleaned_text": None, "text": None, "raw_text": None}, ""),
        ({}, ""),
    ],
)
def test_classification_and_extraction_share_source_contract(values, expected):
    before = dict(values)
    assert select_source_text(values) == expected
    assert CategoryClassificationService._source_text(values) == expected
    assert AIProcessingService._source_text(values) == expected
    assert values == before


@pytest.mark.parametrize(
    "input_value,expected_length",
    [
        (None, 0),
        (100, 0),
        ("", 0),
        ("  emoji  😀 ", len("  emoji  😀 ")),
        ("موفق", len("موفق")),
    ],
)
def test_deterministic_classifier_uses_cleaned_only_and_handles_malformed_values(
    input_value, expected_length
):
    record = ProcessingRecord(data={"cleaned_text": input_value})
    result = ClassifierStage().process(record).data
    assert result["classification"] == "unclassified"
    assert result["classification_text_length"] == expected_length


class RecordingProvider:
    provider = "recording-mock"

    def __init__(self):
        self.classification_calls = []
        self.extraction_calls = []

    def classify_batch(self, messages):
        self.classification_calls.append(
            [(item["message_id"], item["text"]) for item in messages]
        )
        return {int(item["message_id"]): "joblist" for item in messages}

    def extract(self, source_text, category):
        self.extraction_calls.append((source_text, category))
        assert category == "joblist"
        return {
            "category": "joblist",
            "data": {"joblist": {
                "job_title": "Warehouse assistant",
                "description": "Local temporary job",
            }},
        }


@pytest.fixture
def ai_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "ai-fallback.sqlite3"))
    monkeypatch.setattr(config, "AI_CLASSIFICATION_ENABLED", True)
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", False)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED, "joblist", True)
    database.initialize_db()
    return MessageRepository()


def insert_preclassified_candidate(repository, message_id, *, raw_text, text, cleaned_text):
    assert database.insert_message(
        "source", message_id, text, "2026-09-29",
        raw_text=raw_text,
        cleaned_text=cleaned_text,
        processing_status="pending",
    )
    assert repository.mark_processing_result(message_id, "source", success=True)


def test_real_sqlite_classification_and_extraction_preserve_fallback_and_statuses(ai_db):
    # These fixtures represent persisted rows from several versions of Telclaw,
    # including a missing cleaned value and a malformed/non-string cleaned value.
    cases = [
        (101, "  Original raw  ", " legacy ", "  Cleaned text   ✓  ", "Cleaned text   ✓"),
        (102, "raw ignored here", "  Legacy source  ", None, "Legacy source"),
        (103, "  Raw only  🧳  ", None, None, "Raw only  🧳"),
        (104, " Original preserved ", " ", " \t ", "Original preserved"),
        (105, "  Numeric cleaned fallback ", "", 7, "Numeric cleaned fallback"),
        (106, None, None, None, None),
    ]
    for message_id, raw_text, text, cleaned_text, _expected in cases:
        insert_preclassified_candidate(
            ai_db,
            message_id,
            raw_text=raw_text,
            text=text,
            cleaned_text=cleaned_text,
        )
    provider = RecordingProvider()
    classifier = CategoryClassificationService(
        repository=ai_db,
        provider_manager=provider,
        batch_size=20,
    )
    classification_stats = classifier.process_pending()

    assert classification_stats["processed"] == 5
    assert classification_stats["skipped"] == 1
    assert classification_stats["failed"] == 0
    assert provider.classification_calls == [[
        (message_id, expected)
        for message_id, _raw, _legacy, _cleaned, expected in cases
        if expected
    ]]

    extractor = AIProcessingService(repository=ai_db, provider_manager=provider)
    extraction_stats = extractor.process_pending(limit=20)
    assert extraction_stats["processed"] == 5
    assert extraction_stats["skipped"] == 0
    assert extraction_stats["failed"] == 0
    assert provider.extraction_calls == [
        (expected, "joblist")
        for _message_id, _raw, _legacy, _cleaned, expected in cases
        if expected
    ]

    with database.get_connection() as conn:
        records = {
            row["message_id"]: dict(row)
            for row in conn.execute("SELECT * FROM messages ORDER BY message_id")
        }
        jobs = conn.execute("SELECT COUNT(*) FROM joblist").fetchone()[0]

    assert jobs == 5
    for message_id, raw_text, text, cleaned_text, expected in cases:
        record = records[message_id]
        assert record["raw_text"] == raw_text
        assert record["text"] == text
        assert record["cleaned_text"] == cleaned_text
        assert record["classification_attempts"] == 1
        assert record["classification_status"] == "processed"
        if expected:
            assert record["classification_category"] == "joblist"
            assert record["ai_category"] == "joblist"
            assert record["ai_status"] == "processed"
        else:
            assert record["classification_category"] == "none"
            assert record["ai_category"] is None
            assert record["ai_status"] == "skipped"
