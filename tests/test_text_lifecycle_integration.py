"""Full lifecycle contract: Telegram → SQLite → both processing workers → AI → preview.

This module deliberately distinguishes the messages.text compatibility column
from Telegram message.text payloads and formatted publishing-local variables.
The only side effects target temp_path SQLite; all Telegram and AI clients are
in-memory fakes. No production service is started or contacted.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import backoffice_data
import config
import routed_publisher
from ai.ai_service import AIProcessingService
from ai.classification_service import CategoryClassificationService
from collection import crawler
from processing.cleaner import clean_text
from processing.processing_service import ProcessingService as CoreProcessingService
from services.processing_service import ProcessingService as AppProcessingService
from storage import database
from storage.message_repository import MessageRepository


CRAWL_DAY = date(2026, 9, 29)
RAW_ALPHA = (
    "سلام   دنیا 😀\n"
    "ارسال بسته از تهران به تورنتو با پرواز فردا لطفا پیام بدهید"
)
RAW_BETA = (
    "Cargo 📦  departing Berlin tomorrow\n"
    "with room for ten boxes on an international flight please contact me"
)
RAW_LEGACY = (
    "Legacy  message 🧳  from archive\n"
    "with ten or more words and original spacing kept intact"
)


def telegram_message(message_id, original, *, sender_id, photo=False, formatted=None):
    """Use Telegram-style raw_text and text with intentionally different values."""
    return SimpleNamespace(
        id=message_id,
        date=datetime(2026, 9, 29, 10, tzinfo=timezone.utc),
        sender=SimpleNamespace(
            id=sender_id, username=f"user_{sender_id}", bot=False, broadcast=False
        ),
        fwd_from=None,
        forward=None,
        raw_text=original,
        text=formatted if formatted is not None else original,
        message=original,
        media=(
            SimpleNamespace(photo=SimpleNamespace(id=1234 + message_id), document=None)
            if photo else None
        ),
        grouped_id=555 if photo else None,
    )


class FakeTelegram:
    def __init__(self, messages):
        self.messages = list(messages)

    async def get_input_entity(self, channel):
        return SimpleNamespace(channel_id=100, title=f"Fixture {channel}")

    async def iter_messages(self, _entity):
        for message in self.messages:
            yield message


class FakeAIProvider:
    provider = "local-fixture"

    def __init__(self):
        self.classify_calls = []
        self.extract_calls = []

    def classify_batch(self, messages):
        self.classify_calls.append([
            (item["message_id"], item["text"]) for item in messages
        ])
        return {int(item["message_id"]): "joblist" for item in messages}

    def extract(self, text, category):
        self.extract_calls.append((text, category))
        assert category == "joblist"
        return {
            "category": category,
            "data": {"joblist": {
                "job_title": "Warehouse assistant",
                "description": "Apply in person",
            }},
        }


@pytest.fixture
def fixture_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "lifecycle.sqlite3"))
    monkeypatch.setattr(config, "AI_CLASSIFICATION_ENABLED", True)
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", False)
    monkeypatch.setattr(config, "AI_CLASSIFICATION_MAX_RETRIES", 3)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED, "joblist", True)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(crawler.asyncio, "sleep", no_sleep)
    database.initialize_db()
    return MessageRepository()


async def crawl_two_sources():
    """Two channels + exact raw duplicate + media caption + formatted text."""
    first = FakeTelegram([
        telegram_message(
            101, RAW_ALPHA, sender_id=10001,
            formatted="HTML-formatted version must not replace raw",
        ),
        telegram_message(102, RAW_ALPHA, sender_id=10001),
    ])
    second = FakeTelegram([
        telegram_message(201, RAW_BETA, sender_id=20002, photo=True),
    ])
    result_a = await crawler.crawl_channel(first, "alpha_fixture", CRAWL_DAY, CRAWL_DAY)
    result_b = await crawler.crawl_channel(second, "beta_fixture", CRAWL_DAY, CRAWL_DAY)
    return result_a, result_b


def rows_by_channel_and_message():
    with database.get_connection() as conn:
        return {
            (r["channel_username"], r["message_id"]): dict(r)
            for r in conn.execute("SELECT * FROM messages ORDER BY id")
        }


def run_processing(worker):
    if worker == "core":
        return CoreProcessingService().process_pending()
    return AppProcessingService().process_pending_with_stats()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", ["core", "app"])
async def test_crawl_processing_ai_preview_and_editor_are_compatible(
    fixture_db, monkeypatch, worker
):
    alpha, beta = await crawl_two_sources()
    assert (alpha["saved"], alpha["duplicates_skipped"]) == (1, 1)
    assert (beta["saved"], beta["media_saved"]) == (1, 1)

    expected = {
        ("alpha_fixture", 101): RAW_ALPHA,
        ("beta_fixture", 201): RAW_BETA,
    }
    fresh = rows_by_channel_and_message()
    assert set(fresh) == set(expected)
    for key, original in expected.items():
        row = fresh[key]
        assert row["raw_text"] == row["text"] == original
        assert row["cleaned_text"] is None
        assert row["processing_status"] == "pending"
        assert row["ai_status"] == "waiting"
        assert row["collection_status"] == "collected"
    assert fresh["beta_fixture", 201]["media_type"] == "photo"
    assert fresh["beta_fixture", 201]["media_group_id"] == "555"

    processed = run_processing(worker)
    assert processed["processed"] == 2
    assert processed["failed"] == 0
    rows = rows_by_channel_and_message()
    for key, original in expected.items():
        row = rows[key]
        assert row["raw_text"] == original
        assert row["cleaned_text"] == row["text"] == clean_text(original)
        assert row["processing_status"] == "processed"
        # These are distinct existing worker contracts: only the app worker
        # transitions the downstream classifier queue automatically.
        assert row["classification_status"] == (
            "waiting" if worker == "core" else "pending"
        )

    if worker == "core":
        # This core API is intentionally not the scheduler. Explicitly
        # hand off test-only fixtures to classification; no product change.
        for channel, message_id in expected:
            assert database.update_message(
                message_id, channel, classification_status="pending"
            )

    provider = FakeAIProvider()
    classification = CategoryClassificationService(
        repository=fixture_db, provider_manager=provider, batch_size=10
    )
    stats = classification.process_pending()
    assert stats["processed"] == 2 and stats["failed"] == 0
    assert provider.classify_calls == [[
        (101, clean_text(RAW_ALPHA)),
        (201, clean_text(RAW_BETA)),
    ]]

    extraction = AIProcessingService(
        repository=fixture_db, provider_manager=provider, advertio_service=None
    )
    results = extraction.process_pending(limit=10)
    assert results["processed"] == 2 and results["failed"] == 0
    assert provider.extract_calls == [
        (clean_text(RAW_ALPHA), "joblist"),
        (clean_text(RAW_BETA), "joblist"),
    ]

    done = rows_by_channel_and_message()
    for key, original in expected.items():
        row = done[key]
        assert row["raw_text"] == original
        assert row["text"] == row["cleaned_text"] == clean_text(original)
        assert row["classification_category"] == "joblist"
        assert row["classification_status"] == "processed"
        assert row["ai_status"] == "processed"
        assert row["ai_category"] == "joblist"
        with database.get_connection() as conn:
            extracted = dict(conn.execute(
                "SELECT * FROM joblist WHERE processed_message_id=?", (row["id"],)
            ).fetchone())
        assert extracted["job_title"] == "Warehouse assistant"

        # The routed preview receives structured data; it does not publish.
        monkeypatch.setattr(
            routed_publisher.routing_rules, "categories", lambda: ("joblist",)
        )
        monkeypatch.setattr(
            routed_publisher.routing_rules, "category_fields",
            lambda _category: ("job_title", "description")
        )
        preview = routed_publisher._plain_ad({**row, **extracted})
        assert "Warehouse assistant" in preview
        assert "Apply in person" in preview

    # Re-running any stage must not re-queue, re-extract, or send ads.
    assert run_processing(worker)["processed"] == 0
    assert classification.process_pending()["found"] == 0
    assert extraction.process_pending(limit=10)["found"] == 0
    assert len(provider.classify_calls) == 1
    assert len(provider.extract_calls) == 2
    assert rows_by_channel_and_message() == done

    # Backoffice independent edit: cleaned_text takes precedence in AI;
    # legacy text and the untouched Telegram original remain intact.
    backoffice_data.initialize()
    original = done["alpha_fixture", 101]
    edited = "Manually approved AI text while original stays intact"
    assert backoffice_data.update_cell(
        "messages", original["id"], "cleaned_text", edited,
        original["cleaned_text"], 1485409432
    ) == edited
    current = rows_by_channel_and_message()["alpha_fixture", 101]
    assert current["cleaned_text"] == edited
    assert current["text"] == clean_text(RAW_ALPHA)
    assert current["raw_text"] == RAW_ALPHA
    assert AIProcessingService._source_text(current) == edited
    assert CategoryClassificationService._source_text(current) == edited
    assert backoffice_data.page(
        "messages", filters={"cleaned_text": {"op": "contains", "value": "approved AI"}}
    )["total"] == 1
    assert backoffice_data.page(
        "messages", filters={"text": {"op": "contains", "value": "approved AI"}}
    )["total"] == 0
    assert routed_publisher._plain_ad({
        "ai_category": "joblist",
        "job_title": None,
        "description": None,
        **{key: current[key] for key in ("cleaned_text", "raw_text", "text")},
    }) == edited

    # A manual edit must NOT automatically re-run or re-publish existing ads.
    assert classification.process_pending()["found"] == 0
    assert extraction.process_pending(limit=10)["found"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", ["core", "app"])
async def test_legacy_null_source_processes_without_losing_existing_text(
    fixture_db, worker
):
    assert database.insert_message(
        "legacy_fixture", 301, RAW_LEGACY, CRAWL_DAY.isoformat(),
        raw_text=None, cleaned_text=None, sender_id=None,
    )
    start = rows_by_channel_and_message()["legacy_fixture", 301]
    assert start["raw_text"] is None and start["cleaned_text"] is None
    stats = run_processing(worker)
    assert stats["processed"] == 1 and stats["failed"] == 0
    end = rows_by_channel_and_message()["legacy_fixture", 301]
    assert end["raw_text"] is None
    assert end["text"] == end["cleaned_text"] == clean_text(RAW_LEGACY)
    assert AIProcessingService._source_text(end) == clean_text(RAW_LEGACY)


@pytest.mark.asyncio
async def test_ai_classification_retry_does_not_change_preserved_raw_source(
    fixture_db,
):
    assert database.insert_message(
        "retry_fixture", 401, RAW_ALPHA, CRAWL_DAY.isoformat(),
        raw_text=RAW_ALPHA, cleaned_text=None, sender_id=None,
    )
    assert AppProcessingService().process_pending_with_stats()["processed"] == 1

    class FailsOnce(FakeAIProvider):
        def classify_batch(self, messages):
            if not self.classify_calls:
                self.classify_calls.append([
                    (item["message_id"], item["text"]) for item in messages
                ])
                raise RuntimeError("simulated transient classifier failure")
            return super().classify_batch(messages)

    provider = FailsOnce()
    service = CategoryClassificationService(
        repository=fixture_db, provider_manager=provider, batch_size=10
    )
    assert service.process_pending()["failed"] == 1
    failed = rows_by_channel_and_message()["retry_fixture", 401]
    assert failed["classification_status"] == "failed"
    assert failed["classification_attempts"] == 1
    assert failed["raw_text"] == RAW_ALPHA
    assert service.process_pending()["processed"] == 1
    recovered = rows_by_channel_and_message()["retry_fixture", 401]
    assert recovered["classification_status"] == "processed"
    assert recovered["classification_attempts"] == 2
    assert recovered["raw_text"] == RAW_ALPHA
    assert recovered["text"] == recovered["cleaned_text"] == clean_text(RAW_ALPHA)
