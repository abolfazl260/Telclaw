"""Behavioral tests for the current AIProviderManager failover API."""

import pytest

from ai.extractor import AIExtractionError
from ai.provider_manager import AIProviderManager


class Provider:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    def classify_batch(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def extract(self, text, category):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def manager(*providers):
    service = AIProviderManager(provider=providers[0])
    service.providers = list(providers)
    service._unavailable_until = [0.0] * len(providers)
    return service


def test_rate_limit_fails_over_and_honors_retry_after():
    first = Provider("groq", error=AIExtractionError(
        "limited", status=429, reason="rate_limit", retry_after=42
    ))
    second = Provider("cloudflare", result={1: "housinglist"})
    service = manager(first, second)

    assert service.classify_batch([{"message_id": 1, "text": "room"}]) == {1: "housinglist"}
    assert first.calls == 1
    assert second.calls == 1
    assert service.provider == "cloudflare"
    assert service._unavailable_until[0] > 0


def test_next_credential_is_tried_before_next_provider():
    first = Provider("groq-1", error=AIExtractionError(
        "limited", status=429, reason="rate_limit", retry_after=60
    ))
    second = Provider("groq-2", result={1: "none"})
    third = Provider("cloudflare", result={1: "housinglist"})
    service = manager(first, second, third)

    assert service.classify_batch([{"message_id": 1, "text": "chat"}]) == {1: "none"}
    assert third.calls == 0


def test_temporary_failure_continues_to_next_provider():
    first = Provider("groq", error=AIExtractionError("limited", status=429, reason="rate_limit"))
    second = Provider("cloudflare", error=AIExtractionError("upstream", status=503, reason="server_error"))
    third = Provider("future", result={"category": "joblist"})
    assert manager(first, second, third).extract("job", "joblist") == {"category": "joblist"}


def test_invalid_output_does_not_fail_over():
    first = Provider("groq", error=AIExtractionError("bad output", reason="invalid_provider_output"))
    second = Provider("cloudflare", result={1: "housinglist"})

    with pytest.raises(AIExtractionError, match="bad output"):
        manager(first, second).classify_batch([{"message_id": 1, "text": "room"}])
    assert second.calls == 0


def test_highest_priority_provider_recovers_after_cooldown():
    first = Provider("groq", result={1: "housinglist"})
    second = Provider("cloudflare", result={1: "none"})
    service = manager(first, second)
    service.active_index = 1
    service._unavailable_until[0] = 0.0
    service._last_recovery_check = -1e9

    assert service.classify_batch([{"message_id": 1, "text": "room"}]) == {1: "housinglist"}
    assert service.active_index == 0
