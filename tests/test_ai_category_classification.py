import importlib
import sqlite3
def _load_database(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("GROQ_API_KEY", "key")
    monkeypatch.setenv("TELCLAW_GROQ_MODEL", "model")
    monkeypatch.setenv("TELCLAW_AI_CLASSIFICATION_ENABLED", "true")
    config = importlib.import_module("config")
    config.DB_NAME = str(tmp_path / "telclaw.db")
    config.AI_CLASSIFICATION_ENABLED = True
    config.AI_CLASSIFICATION_BATCH_SIZE = 50
    database = importlib.import_module("storage.database")
    database.config.DB_NAME = config.DB_NAME
    return database, config


class FakeBatchClassifier:
    def __init__(self):
        self.calls = []

    def classify_batch(self, messages):
        self.calls.append(messages)
        return {int(item["message_id"]): "housinglist" if int(item["message_id"]) == 101 else "none" for item in messages}


class FailingBatchClassifier:
    def classify_batch(self, messages):
        raise RuntimeError("classifier is unavailable")


def test_processing_success_enqueues_classification(monkeypatch, tmp_path):
    database, _ = _load_database(monkeypatch, tmp_path)
    from storage.message_repository import MessageRepository

    database.initialize_db()
    database.insert_message("chan", 101, "Room for rent", "2026-08-27")
    repo = MessageRepository()

    repo.mark_processing_result(101, "chan", success=True, text="Room for rent", cleaned_text="Room for rent")

    record = repo.get_classification_pending(limit=10)[0]
    assert record["message_id"] == 101
    assert record["classification_status"] == "pending"
    assert record["ai_status"] == "waiting"


def test_category_classification_service_batches_and_marks_results(monkeypatch, tmp_path):
    database, config = _load_database(monkeypatch, tmp_path)
    from ai.classification_service import CategoryClassificationService
    from storage.message_repository import MessageRepository

    database.initialize_db()
    database.insert_message("chan", 101, "Room for rent", "2026-08-27")
    database.insert_message("chan", 102, "random chat", "2026-08-27")
    repo = MessageRepository()
    repo.mark_processing_result(101, "chan", success=True, text="Room for rent", cleaned_text="Room for rent")
    repo.mark_processing_result(102, "chan", success=True, text="random chat", cleaned_text="random chat")

    classifier = FakeBatchClassifier()
    service = CategoryClassificationService(repository=repo, classifier=classifier, batch_size=config.AI_CLASSIFICATION_BATCH_SIZE)

    stats = service.process_pending()

    assert stats == {"found": 2, "processed": 2, "failed": 0, "skipped": 0, "stopped": False, "disabled": False}
    assert [[item["message_id"] for item in call] for call in classifier.calls] == [[101, 102]]
    with sqlite3.connect(config.DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        rows = {row["message_id"]: dict(row) for row in conn.execute("SELECT message_id, classification_status, classification_category, ai_category, ai_status FROM messages")}
    assert rows[101]["classification_status"] == "processed"
    assert rows[101]["classification_category"] == "housinglist"
    assert rows[101]["ai_category"] == "housinglist"
    assert rows[101]["ai_status"] == "pending"
    assert rows[102]["classification_category"] == "none"
    assert rows[102]["ai_category"] is None
    assert rows[102]["ai_status"] == "skipped"


def test_category_classification_drains_multiple_batches(monkeypatch, tmp_path):
    database, config = _load_database(monkeypatch, tmp_path)
    from ai.classification_service import CategoryClassificationService
    from storage.message_repository import MessageRepository

    database.initialize_db()
    repo = MessageRepository()
    for message_id in range(101, 126):
        database.insert_message("chan", message_id, f"message {message_id}", "2026-08-27")
        repo.mark_processing_result(
            message_id,
            "chan",
            success=True,
            text=f"message {message_id}",
            cleaned_text=f"message {message_id}",
        )

    classifier = FakeBatchClassifier()
    service = CategoryClassificationService(
        repository=repo,
        classifier=classifier,
        batch_size=10,
    )
    stats = service.process_pending_with_stats()

    assert stats["found"] == 25
    assert stats["processed"] == 25
    assert stats["failed"] == 0
    assert stats["remaining"] == 0
    assert [len(call) for call in classifier.calls] == [10, 10, 5]


def test_runtime_batch_size_is_resolved_for_existing_service(monkeypatch, tmp_path):
    database, config = _load_database(monkeypatch, tmp_path)
    from ai.classification_service import CategoryClassificationService
    from storage.message_repository import MessageRepository

    database.initialize_db()
    repo = MessageRepository()
    for message_id in range(201, 213):
        database.insert_message("chan", message_id, f"message {message_id}", "2026-08-27")
        repo.mark_processing_result(
            message_id,
            "chan",
            success=True,
            text=f"message {message_id}",
            cleaned_text=f"message {message_id}",
        )

    classifier = FakeBatchClassifier()
    service = CategoryClassificationService(repository=repo, classifier=classifier)
    config.AI_CLASSIFICATION_BATCH_SIZE = 5

    stats = service.process_pending_with_stats()

    assert stats["remaining"] == 0
    assert [len(call) for call in classifier.calls] == [5, 5, 2]


def test_failed_classification_batch_is_deferred_to_next_cycle(monkeypatch, tmp_path):
    database, config = _load_database(monkeypatch, tmp_path)
    from ai.classification_service import CategoryClassificationService
    from storage.message_repository import MessageRepository

    database.initialize_db()
    repo = MessageRepository()
    for message_id in (301, 302):
        database.insert_message("chan", message_id, "message", "2026-08-27")
        repo.mark_processing_result(
            message_id,
            "chan",
            success=True,
            text="message",
            cleaned_text="message",
        )

    classifier = FailingBatchClassifier()
    service = CategoryClassificationService(
        repository=repo,
        classifier=classifier,
        batch_size=10,
    )
    stats = service.process_pending_with_stats()

    assert stats["found"] == 2
    assert stats["failed"] == 2
    assert stats["remaining"] == 2
    with database.get_connection() as conn:
        attempts = [
            row["classification_attempts"]
            for row in conn.execute(
                "SELECT classification_attempts FROM messages ORDER BY message_id"
            ).fetchall()
        ]
    assert attempts == [1, 1]


def test_category_classification_skips_messages_that_already_have_ai_category(monkeypatch, tmp_path):
    database, config = _load_database(monkeypatch, tmp_path)
    from ai.classification_service import CategoryClassificationService
    from storage.message_repository import MessageRepository

    database.initialize_db()
    database.insert_message("chan", 101, "Room for rent", "2026-08-27")
    database.insert_message("chan", 102, "Job available", "2026-08-27")
    repo = MessageRepository()
    for message_id in (101, 102):
        repo.mark_processing_result(message_id, "chan", success=True, text="listing", cleaned_text="listing")
    repo.update_message(101, "chan", ai_category="housinglist")

    classifier = FakeBatchClassifier()
    stats = CategoryClassificationService(repository=repo, classifier=classifier, batch_size=config.AI_CLASSIFICATION_BATCH_SIZE).process_pending()

    assert stats["found"] == 1
    assert [[item["message_id"] for item in call] for call in classifier.calls] == [[102]]
    with database.get_connection() as conn:
        row = conn.execute("SELECT ai_category FROM messages WHERE message_id=101").fetchone()
    assert row["ai_category"] == "housinglist"


def test_category_classification_errors_are_logged_and_printed(monkeypatch, tmp_path, caplog, capsys):
    database, config = _load_database(monkeypatch, tmp_path)
    from ai.classification_service import CategoryClassificationService
    from storage.message_repository import MessageRepository

    database.initialize_db()
    database.insert_message("chan", 101, "Room for rent", "2026-08-27")
    repo = MessageRepository()
    repo.mark_processing_result(101, "chan", success=True, text="Room for rent", cleaned_text="Room for rent")

    with caplog.at_level("ERROR", logger="telclaw.ai.classification"):
        stats = CategoryClassificationService(repository=repo, classifier=FailingBatchClassifier(), batch_size=config.AI_CLASSIFICATION_BATCH_SIZE).process_pending()

    assert stats["failed"] == 1
    assert "failed whole batch: classifier is unavailable" in caplog.text
    assert "[AI CLASSIFICATION ERROR] failed whole batch: classifier is unavailable" in capsys.readouterr().out


def test_classification_queue_status_and_manual_retry_are_database_backed(monkeypatch, tmp_path):
    database, _ = _load_database(monkeypatch, tmp_path)

    database.initialize_db()
    for message_id in range(101, 105):
        database.insert_message("chan", message_id, "message", "2026-08-27")

    with database.get_connection() as conn:
        conn.execute("UPDATE messages SET classification_status='pending' WHERE message_id=101")
        conn.execute("UPDATE messages SET classification_status='processing' WHERE message_id=102")
        conn.execute("UPDATE messages SET classification_status='processed' WHERE message_id=103")
        conn.execute("""UPDATE messages
            SET classification_status='failed', classification_error='provider unavailable',
                classification_attempts=3, classification_processed_at='2026-08-27T00:00:00Z'
            WHERE message_id=104""")
        conn.commit()

    assert database.get_classification_queue_status() == {
        "pending": 1,
        "processing": 1,
        "classified": 1,
        "failed": 1,
    }
    assert database.retry_failed_classifications() == 1
    assert database.get_classification_queue_status() == {
        "pending": 2,
        "processing": 1,
        "classified": 1,
        "failed": 0,
    }
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT classification_error, classification_attempts, classification_processed_at "
            "FROM messages WHERE message_id=104"
        ).fetchone()
    assert dict(row) == {
        "classification_error": None,
        "classification_attempts": 0,
        "classification_processed_at": None,
    }


def test_validate_classification_result_rejects_unknown_category():
    from ai.category_classifier import validate_classification_result

    try:
        validate_classification_result({"classifications": [{"message_id": 101, "category": "cars"}]}, [101])
    except ValueError as exc:
        assert "unsupported classification category" in str(exc)
    else:
        raise AssertionError("unknown category should fail validation")


class InvalidOutputClassifier:
    """A single bad response must not keep unrelated messages from processing."""

    def __init__(self):
        self.calls = []
        self.broken = True

    def classify_batch(self, messages):
        from ai.extractor import AIExtractionError

        ids = [int(item["message_id"]) for item in messages]
        self.calls.append(ids)
        if self.broken and 302 in ids:
            raise AIExtractionError(
                "Invalid Cloudflare JSON output",
                provider="cloudflare",
                reason="invalid_provider_output",
            )
        return {message_id: "housinglist" for message_id in ids}


def test_invalid_provider_output_splits_batch_and_retries_only_failed_message(monkeypatch, tmp_path):
    database, config = _load_database(monkeypatch, tmp_path)
    from ai.classification_service import CategoryClassificationService
    from storage.message_repository import MessageRepository

    monkeypatch.setattr(config, "GROQ_INVALID_JSON_MAX_RETRIES", 0)
    database.initialize_db()
    repo = MessageRepository()
    for message_id in (301, 302, 303):
        database.insert_message("chan", message_id, f"message {message_id}", "2026-08-27")
        repo.mark_processing_result(
            message_id, "chan", success=True,
            text=f"message {message_id}", cleaned_text=f"message {message_id}",
        )
    classifier = InvalidOutputClassifier()
    service = CategoryClassificationService(repository=repo, classifier=classifier, batch_size=10)

    first = service.process_pending_with_stats()

    assert first["processed"] == 2
    assert first["failed"] == 1
    assert first["remaining"] == 1
    assert [301, 302, 303] in classifier.calls
    assert [302] in classifier.calls
    with database.get_connection() as conn:
        rows = {r["message_id"]: dict(r) for r in conn.execute(
            "SELECT message_id, text, classification_status, classification_error, "
            "classification_attempts FROM messages ORDER BY message_id"
        )}
    assert rows[301]["classification_status"] == "processed"
    assert rows[303]["classification_status"] == "processed"
    assert rows[302]["classification_status"] == "failed"
    assert rows[302]["classification_error"].startswith("invalid_provider_output:")
    assert rows[302]["classification_attempts"] == 1
    assert rows[302]["text"] == "message 302"

    # Failed message remains eligible until the regular retry budget is exhausted.
    classifier.broken = False
    second = service.process_pending_with_stats()
    assert second["processed"] == 1
    assert second["failed"] == 0
    assert second["remaining"] == 0
    assert classifier.calls[-1] == [302]


def test_requeue_only_invalid_provider_output_including_exhausted_legacy_errors(monkeypatch, tmp_path):
    database, config = _load_database(monkeypatch, tmp_path)
    from storage.message_repository import MessageRepository

    database.initialize_db()
    repo = MessageRepository()
    failures = {
        401: "invalid_provider_output: Invalid Cloudflare JSON output",
        402: "Invalid Cloudflare JSON output",
        403: "Invalid Groq classification JSON output: malformed",
        404: "rate_limit: provider unavailable",
    }
    for message_id, error in failures.items():
        database.insert_message("chan", message_id, "message", "2026-08-27")
        repo.mark_processing_result(message_id, "chan", success=True, cleaned_text="message")
        repo.mark_classification_result(
            message_id, "chan", success=False,
            error=error, attempts=config.AI_CLASSIFICATION_MAX_RETRIES,
            processed_at="2026-08-27T00:00:00+00:00",
        )

    # None are eligible for the ordinary automatic retry after exhaustion.
    assert repo.get_classification_pending(limit=20) == []
    assert repo.retry_invalid_provider_output_classifications() == 3

    eligible = repo.get_classification_pending(limit=20)
    assert {record["message_id"] for record in eligible} == {401, 402, 403}
    with database.get_connection() as conn:
        rows = {r["message_id"]: dict(r) for r in conn.execute(
            "SELECT message_id, classification_status, classification_error, "
            "classification_attempts, classification_processed_at FROM messages"
        )}
    for message_id in (401, 402, 403):
        assert rows[message_id]["classification_status"] == "pending"
        assert rows[message_id]["classification_error"] is None
        assert rows[message_id]["classification_attempts"] == 0
        assert rows[message_id]["classification_processed_at"] is None
    assert rows[404]["classification_status"] == "failed"
    assert rows[404]["classification_error"] == "rate_limit: provider unavailable"
    assert repo.retry_invalid_provider_output_classifications() == 0


def test_jobs_classifier_includes_requests_to_engage_professionals():
    from ai.category_classifier import build_classification_prompt

    prompt = build_classification_prompt()
    assert "joblist" in prompt
    assert "engage an individual worker or professional" in prompt
    assert "one-off work without an employment contract" in prompt
    assert "Exclude standalone business/service promotions" in prompt
    assert "Link-only vacancy teasers" in prompt
    assert "Do not treat recruiters or vacancy feeds as job seekers." in prompt
