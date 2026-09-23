from datetime import datetime, timezone
import threading

import pytest

from ai.extractor import AIExtractionError
from ai.provider_manager import AIProviderManager, _ProviderState


class Provider:
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error = name, result, error
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
    return AIProviderManager(provider_states=[
        _ProviderState(f"{provider.name}-{index}", provider.name, provider)
        for index, provider in enumerate(providers, 1)
    ])


def test_rate_limit_fails_over_and_records_retry_after():
    groq = Provider("groq", error=AIExtractionError("limited", status=429, reason="rate_limit", retry_after=42))
    cloudflare = Provider("cloudflare", result={1: "housinglist"})
    service = manager(groq, cloudflare)

    assert service.classify_batch([{"message_id": 1, "text": "room"}]) == {1: "housinglist"}
    status = service.get_status()["groq-1"]
    assert status["available"] is False
    assert status["reason"] == "rate_limit"
    assert datetime.fromisoformat(status["cooldown_until"]).astimezone(timezone.utc) > datetime.now(timezone.utc)


def test_same_provider_next_credential_is_used_before_next_provider():
    first = Provider("groq", error=AIExtractionError("limited", status=429, reason="rate_limit", retry_after=60))
    second = Provider("groq", result={1: "none"})
    cloudflare = Provider("cloudflare", result={1: "housinglist"})
    service = manager(first, second, cloudflare)

    assert service.classify_batch([{"message_id": 1, "text": "chat"}]) == {1: "none"}
    assert cloudflare.calls == 0


def test_temporary_provider_chain_continues_to_next_available_provider():
    groq = Provider("groq", error=AIExtractionError("limited", status=429, reason="rate_limit"))
    cloudflare = Provider("cloudflare", error=AIExtractionError("upstream", status=503, reason="server_error"))
    next_provider = Provider("future", result={"category": "joblist"})
    assert manager(groq, cloudflare, next_provider).extract("job", "joblist") == {"category": "joblist"}


def test_all_failures_return_controlled_retryable_error():
    service = manager(Provider("groq", error=AIExtractionError("down", status=503, reason="server_error")))
    with pytest.raises(AIExtractionError) as raised:
        service.classify_batch([{"message_id": 1, "text": "room"}])
    assert raised.value.reason == "all_providers_unavailable"


def test_invalid_json_does_not_fail_over():
    first = Provider("groq", error=AIExtractionError("bad output", reason="invalid_provider_output"))
    second = Provider("cloudflare", result={1: "housinglist"})
    with pytest.raises(AIExtractionError, match="bad output"):
        manager(first, second).classify_batch([{"message_id": 1, "text": "room"}])
    assert second.calls == 0


def test_expired_cooldown_recovers_highest_priority_provider():
    first = Provider("groq", result={1: "housinglist"})
    second = Provider("cloudflare", result={1: "none"})
    service = manager(first, second)
    state = service._states[0]
    state.available = False
    state.cooldown_monotonic = 0
    state.cooldown_until = datetime.now(timezone.utc)
    assert service.classify_batch([{"message_id": 1, "text": "room"}]) == {1: "housinglist"}
    assert service.get_status()["groq-1"]["available"] is True


def test_concurrent_failover_updates_state_once_and_uses_healthy_provider():
    first = Provider("groq", error=AIExtractionError("limited", status=429, reason="rate_limit", retry_after=30))
    second = Provider("cloudflare", result={1: "housinglist"})
    service = manager(first, second)
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.classify_batch([{"message_id": 1, "text": "room"}]))) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert results == [{1: "housinglist"}] * 4
    assert service.get_status()["groq-1"]["available"] is False
