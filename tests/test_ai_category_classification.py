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


def test_validate_classification_result_rejects_unknown_category():
    from ai.category_classifier import validate_classification_result

    try:
        validate_classification_result({"classifications": [{"message_id": 101, "category": "cars"}]}, [101])
    except ValueError as exc:
        assert "unsupported classification category" in str(exc)
    else:
        raise AssertionError("unknown category should fail validation")
