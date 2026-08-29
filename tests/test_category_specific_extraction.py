import json
import os

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("TELCLAW_GROQ_MODEL", "test-model")
os.environ.setdefault("TELCLAW_AI_EXTRACTION_ENABLED", "true")
os.environ.setdefault("TELCLAW_AI_CLASSIFICATION_ENABLED", "true")

import config
from ai.ai_service import AIProcessingService
from ai.category_schemas import CATEGORIES
from ai.classification_service import CategoryClassificationService
from ai.extractor import GroqExtractor
from ai.prompt_loader import PROMPT_FILES, PROMPT_DIR, load_prompt, render_prompt


class FakeRateLimiter:
    def wait(self):
        return None

    def slot(self):
        class Slot:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return Slot()


class FakeResponse:
    ok = True
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeRepository:
    def __init__(self):
        self.skipped = []
        self.classified = []
        self.processing = []
        self.saved = []
        self.results = []

    def mark_ai_skipped(self, message_id, channel_username, **fields):
        self.skipped.append((message_id, channel_username, fields))

    def mark_ai_processing(self, message_id, channel_username):
        self.processing.append((message_id, channel_username))

    def mark_ai_result(self, **fields):
        self.results.append(fields)

    def save_category_record(self, processed_message_id, category, data):
        self.saved.append((processed_message_id, category, data))

    def mark_classification_processing(self, message_id, channel_username):
        self.processing.append((message_id, channel_username))

    def mark_classification_result(self, message_id, channel_username, **fields):
        self.classified.append((message_id, channel_username, fields))

    def get_classification_pending(self, **kwargs):
        return []


def _extractor(monkeypatch, category):
    captured = {}

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        data = {field: None for field in (
            "title", "description", "location", "country_code", "province", "city", "neighborhood",
            "price", "currency", "rent_period", "bedrooms", "bathrooms", "area", "area_unit",
            "furnished", "availability", "property_condition", "contact", "features",
        )}
        data["title"] = "Test Listing"
        data["country_code"] = "CA"
        data["currency"] = "CAD"
        if category == "housinglist":
            data.update({"property_type": "apartment", "listing_type": "rent"})
        payload = {"choices": [{"message": {"content": json.dumps({"category": category, "data": {category: data}})}}]}
        return FakeResponse(payload)

    monkeypatch.setattr("ai.extractor.requests.post", fake_post)
    extractor = GroqExtractor(api_key="test-key", model="test-model", rate_limiter=FakeRateLimiter())
    return extractor, captured


def test_housing_uses_only_housing_prompt(monkeypatch):
    extractor, captured = _extractor(monkeypatch, "housinglist")
    extractor.extract("2 bedroom apartment in Toronto for CAD 2500", category="housinglist")
    prompt = captured["json"]["messages"][0]["content"]
    assert "housing extraction worker" in prompt
    assert "property_type" in prompt
    assert "job_title" not in prompt
    assert "origin_city" not in prompt


def test_job_uses_only_job_prompt():
    prompt = render_prompt("joblist")
    assert "job extraction worker" in prompt
    assert "job_title" in prompt
    assert "property_type" not in prompt
    assert "origin_city" not in prompt


def test_transfer_disabled_skips_without_provider_call(monkeypatch):
    repo = FakeRepository()
    calls = []

    class FakeExtractor:
        provider = "groq-1"
        model = "test-model"

        def extract(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("disabled category must not call Groq")

    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", True)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED, "transferlist", False)
    service = AIProcessingService(repository=repo, extractor=FakeExtractor())
    stats = service._process([{"id": 1, "message_id": 10, "channel_username": "test", "cleaned_text": "cargo"}])

    assert stats["processed"] == 0
    assert stats["failed"] == 0
    assert stats["skipped"] == 1
    assert calls == []
    assert repo.skipped[0][2]["reason"] == "category_disabled:transferlist"


def test_classification_remains_independent_when_extraction_disabled(monkeypatch):
    repo = FakeRepository()

    class FakeClassifier:
        def classify_batch(self, batch):
            return {10: "transferlist"}

    monkeypatch.setattr(config, "AI_CLASSIFICATION_ENABLED", True)
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", False)
    monkeypatch.update = getattr(monkeypatch, "update", None)
    service = CategoryClassificationService(repository=repo, classifier=FakeClassifier())
    result = service._process_batch([{"message_id": 10, "channel_username": "test", "cleaned_text": "cargo from Toronto"}])

    assert result["processed"] == 1
    assert repo.classified[0][2]["category"] == "transferlist"
    assert CATEGORIES == ("housinglist", "transferlist", "joblist")


def test_prompt_isolation():
    prompts = {category: load_prompt(category) for category in PROMPT_FILES}
    assert "housinglist" not in prompts["joblist"]
    assert "origin_city" not in prompts["housinglist"]
    assert "job_title" not in prompts["transferlist"]
    assert "property_type" not in prompts["joblist"]


def test_missing_prompt_is_configuration_error(monkeypatch, tmp_path):
    import ai.prompt_loader as prompt_loader

    monkeypatch.setattr(prompt_loader, "PROMPT_DIR", tmp_path)
    with __import__("pytest").raises(RuntimeError, match="Missing extraction prompt.*housinglist"):
        prompt_loader.load_prompt("housinglist")
