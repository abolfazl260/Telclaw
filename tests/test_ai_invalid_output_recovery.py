"""Regression coverage for bounded invalid-JSON extraction failover.

These tests use fake providers; no Groq or Cloudflare network request is made.
"""

import time

import pytest

import config
from ai.ai_service import AIProcessingService
from ai.extractor import AIExtractionError
from ai.provider_manager import AIProviderManager


def invalid_json(provider):
    return AIExtractionError(
        "invalid JSON from " + provider,
        provider=provider,
        status=400,
        reason="invalid_provider_output",
    )


class StubProvider:
    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = []

    def extract(self, text, category):
        self.calls.append((text, category))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def classify_batch(self, messages):
        self.calls.append(messages)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_manager(*providers):
    result = AIProviderManager(provider=providers[0])
    result.providers = list(providers)
    result._unavailable_until = [0.0] * len(providers)
    return result


def make_service(manager, repository=None):
    service = object.__new__(AIProcessingService)
    service.provider_manager = manager
    service.repository = repository
    service.advertio_service = None
    service.media_downloader = None
    return service


def test_first_invalid_provider_uses_alternate_once_without_cooldown():
    result = {"category": "transferlist", "data": {"transferlist": {"title": "Cargo"}}}
    primary = StubProvider("groq", [invalid_json("groq")])
    alternate = StubProvider("cloudflare", [result])
    manager = make_manager(primary, alternate)

    assert manager.extract("cargo listing", "transferlist") == result
    assert len(primary.calls) == len(alternate.calls) == 1
    assert manager.active_index == 0
    assert manager._unavailable_until == [0.0, 0.0]


def test_both_invalid_then_service_retries_only_primary(monkeypatch):
    monkeypatch.setattr(config, "GROQ_INVALID_JSON_MAX_RETRIES", 1)
    primary = StubProvider("groq", [invalid_json("groq"), invalid_json("groq")])
    alternate = StubProvider("cloudflare", [invalid_json("cloudflare")])
    manager = make_manager(primary, alternate)
    service = make_service(manager)

    with pytest.raises(AIExtractionError) as caught:
        service._extract_with_retry(
            "cargo listing",
            {"message_id": 21065, "channel_username": "china_chamedan"},
            "transferlist",
        )

    assert caught.value.reason == "invalid_provider_output"
    assert [len(primary.calls), len(alternate.calls)] == [2, 1]
    assert manager._unavailable_until == [0.0, 0.0]


def test_single_invalid_provider_keeps_bounded_retry(monkeypatch):
    monkeypatch.setattr(config, "GROQ_INVALID_JSON_MAX_RETRIES", 1)
    primary = StubProvider("groq", [invalid_json("groq"), invalid_json("groq")])
    service = make_service(make_manager(primary))

    with pytest.raises(AIExtractionError, match="invalid JSON"):
        service._extract_with_retry(
            "listing", {"message_id": 1, "channel_username": "sample"}, "joblist"
        )

    assert len(primary.calls) == 2


def test_no_fallback_when_explicitly_disabled():
    primary = StubProvider("groq", [invalid_json("groq")])
    alternate = StubProvider("cloudflare", [{"category": "joblist"}])
    manager = make_manager(primary, alternate)

    with pytest.raises(AIExtractionError) as caught:
        manager.extract("job listing", "joblist", allow_invalid_output_fallback=False)

    assert caught.value.reason == "invalid_provider_output"
    assert len(primary.calls) == 1
    assert len(alternate.calls) == 0


def test_rate_limited_alternate_is_cooled_down_and_error_preserved():
    primary = StubProvider("groq", [invalid_json("groq")])
    alternate = StubProvider("cloudflare", [
        AIExtractionError("rate limited", provider="cloudflare",
                          status=429, reason="rate_limit", retry_after=20)
    ])
    manager = make_manager(primary, alternate)

    with pytest.raises(AIExtractionError) as caught:
        manager.extract("cargo listing", "transferlist")

    assert caught.value.status == 429
    assert caught.value.reason == "rate_limit"
    assert caught.value.retry_after == 20
    assert manager._unavailable_until[0] == 0
    assert manager._unavailable_until[1] > time.monotonic()


def test_fatal_alternate_error_is_not_rewritten_as_bad_json():
    primary = StubProvider("groq", [invalid_json("groq")])
    alternate = StubProvider("cloudflare", [
        AIExtractionError("access denied", provider="cloudflare",
                          status=403, reason="permissions_error", stop_queue=True)
    ])

    with pytest.raises(AIExtractionError) as caught:
        make_manager(primary, alternate).extract("listing", "joblist")

    assert caught.value.stop_queue is True
    assert caught.value.status == 403
    assert caught.value.reason == "permissions_error"


def test_classification_does_not_use_invalid_output_fallback():
    primary = StubProvider("groq", [invalid_json("groq")])
    alternate = StubProvider("cloudflare", [{"1": "joblist"}])
    manager = make_manager(primary, alternate)

    with pytest.raises(AIExtractionError):
        manager.classify_batch([{"message_id": 1, "text": "job"}])

    assert len(primary.calls) == 1
    assert alternate.calls == []


class StubRepository:
    def __init__(self):
        self.results = []
        self.saved = []

    def mark_ai_processing(self, message_id, channel_username):
        return True

    def mark_ai_result(self, **kwargs):
        self.results.append(kwargs)

    def save_category_record(self, *args):
        self.saved.append(args)


def test_exhausted_invalid_json_never_saves_partial_ad(monkeypatch):
    monkeypatch.setattr(config, "GROQ_INVALID_JSON_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", True)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED, "transferlist", True)
    primary = StubProvider("groq", [invalid_json("groq"), invalid_json("groq")])
    alternate = StubProvider("cloudflare", [invalid_json("cloudflare")])
    repository = StubRepository()
    service = make_service(make_manager(primary, alternate), repository)

    stats = service._process([{
        "id": 42,
        "message_id": 21065,
        "channel_username": "china_chamedan",
        "classification_category": "transferlist",
        "cleaned_text": "Shipping cargo from Tehran to Toronto",
    }])

    assert stats[0] == 0  # processed
    assert stats[1] == 1  # failed
    assert repository.saved == []
    assert len(repository.results) == 1
    assert repository.results[0]["success"] is False
    assert repository.results[0]["ai_error"] == "invalid_provider_output"
    assert [len(primary.calls), len(alternate.calls)] == [2, 1]
