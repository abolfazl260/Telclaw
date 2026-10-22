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

def test_all_rate_limited_providers_are_tried_once_and_return_earliest_retry(monkeypatch):
    import config

    now = [100.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: now[0])
    monkeypatch.setattr(config, "AI_RETRY_COUNT", 3)
    first = Provider("groq", error=AIExtractionError(
        "limited", status=429, reason="rate_limit", retry_after=30
    ))
    second = Provider("cloudflare", error=AIExtractionError(
        "limited", status=429, reason="rate_limit", retry_after=5
    ))
    service = manager(first, second)

    with pytest.raises(AIExtractionError) as raised:
        service.extract("listing", "housinglist")

    assert raised.value.status == 429
    assert raised.value.reason == "rate_limit"
    assert raised.value.retry_after == pytest.approx(5)
    assert (first.calls, second.calls) == (1, 1)
    assert service._unavailable_until == [130.0, 105.0]

    # A repeated queue attempt before the deadline must not send HTTP traffic.
    with pytest.raises(AIExtractionError) as during_cooldown:
        service.extract("listing", "housinglist")
    assert during_cooldown.value.retry_after == pytest.approx(5)
    assert (first.calls, second.calls) == (1, 1)

    now[0] = 106
    second.error = None
    second.result = {"category": "housinglist"}
    assert service.extract("listing", "housinglist") == {"category": "housinglist"}
    assert (first.calls, second.calls) == (1, 2)


def test_single_provider_cools_down_instead_of_immediate_retries(monkeypatch):
    import config

    now = [200.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: now[0])
    monkeypatch.setattr(config, "AI_RETRY_COUNT", 5)
    only = Provider("groq", error=AIExtractionError(
        "limited", status=429, reason="rate_limit", retry_after=10
    ))
    service = manager(only)

    for _ in range(3):
        with pytest.raises(AIExtractionError) as raised:
            service.classify_batch([{"message_id": 1, "text": "room"}])
        assert raised.value.status == 429
        assert raised.value.retry_after == pytest.approx(10)
    assert only.calls == 1

    now[0] = 211.0
    only.error = None
    only.result = {1: "housinglist"}
    assert service.classify_batch([{"message_id": 1, "text": "room"}]) == {1: "housinglist"}
    assert only.calls == 2


def test_cooling_active_provider_is_skipped_even_within_recovery_interval(monkeypatch):
    import config

    now = [100.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: now[0])
    monkeypatch.setattr(config, "AI_RECOVERY_INTERVAL_SECONDS", 3600)
    first = Provider("groq", result={1: "none"})
    second = Provider("cloudflare", result={1: "housinglist"})
    service = manager(first, second)
    service.active_index = 0
    service._last_recovery_check = 100.0
    service._unavailable_until[0] = 140.0

    assert service.classify_batch([{"message_id": 1, "text": "room"}]) == {1: "housinglist"}
    assert first.calls == 0
    assert second.calls == 1


def test_zero_retry_after_uses_configured_cooldown(monkeypatch):
    import config

    now = [100.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: now[0])
    monkeypatch.setattr(config, "AI_COOLDOWN_SECONDS", 17)
    only = Provider("groq", error=AIExtractionError(
        "limited", status=429, reason="rate_limit", retry_after=0
    ))
    service = manager(only)

    with pytest.raises(AIExtractionError) as raised:
        service.extract("listing", "housinglist")
    assert raised.value.retry_after == pytest.approx(17)
    assert only.calls == 1


def test_permission_error_preserves_status_and_stop_queue_during_cooldown(monkeypatch):
    import config

    now = [100.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: now[0])
    monkeypatch.setattr(config, "AI_RETRY_COUNT", 3)
    first = Provider("groq", error=AIExtractionError(
        "forbidden", status=403, reason="permissions_error", stop_queue=True
    ))
    service = manager(first)

    for _ in range(2):
        with pytest.raises(AIExtractionError) as raised:
            service.extract("listing", "housinglist")
        assert raised.value.status == 403
        assert raised.value.stop_queue is True
    assert first.calls == 1


def test_invalid_extraction_json_recovers_through_alternate_provider():
    primary = Provider("cloudflare", error=AIExtractionError(
        "malformed json", reason="invalid_provider_output"
    ))
    alternate = Provider("groq", result={
        "category": "transferlist", "data": {"transferlist": {"title": "Cargo"}}
    })
    service = manager(primary, alternate)
    original_cooldowns = list(service._unavailable_until)

    result = service.extract("Tehran to Toronto luggage", "transferlist")

    assert result["category"] == "transferlist"
    assert primary.calls == 1
    assert alternate.calls == 1
    # Malformed content is not evidence of a global provider outage.
    assert service.active_index == 0
    assert service._unavailable_until == original_cooldowns


def test_all_invalid_extraction_providers_are_tried_once_not_repaired():
    first = Provider("cloudflare", error=AIExtractionError(
        "bad primary json", reason="invalid_provider_output"
    ))
    second = Provider("groq", error=AIExtractionError(
        "bad alternate json", reason="invalid_provider_output"
    ))
    third = Provider("other", error=AIExtractionError(
        "bad third json", reason="invalid_provider_output"
    ))
    service = manager(first, second, third)

    with pytest.raises(AIExtractionError, match="bad third json"):
        service.extract("unclear trip", "transferlist")
    assert [p.calls for p in (first, second, third)] == [1, 1, 1]


def test_invalid_extraction_skips_cooling_alternates(monkeypatch):
    import time
    first = Provider("cloudflare", error=AIExtractionError(
        "invalid json", reason="invalid_provider_output"
    ))
    cooling = Provider("groq", result={"category": "transferlist"})
    available = Provider("other", result={"category": "transferlist"})
    service = manager(first, cooling, available)
    service._unavailable_until[1] = time.monotonic() + 120

    assert service.extract("some cargo ad", "transferlist") == {"category": "transferlist"}
    assert first.calls == 1
    assert cooling.calls == 0
    assert available.calls == 1


def test_invalid_extraction_does_not_mask_alternate_rate_limit():
    first = Provider("cloudflare", error=AIExtractionError(
        "bad json", reason="invalid_provider_output"
    ))
    alternate = Provider("groq", error=AIExtractionError(
        "rate limited", reason="rate_limit", status=429, retry_after=10
    ))
    with pytest.raises(AIExtractionError) as raised:
        manager(first, alternate).extract("some cargo ad", "transferlist")
    assert raised.value.status == 429
    assert raised.value.retry_after == 10


def test_invalid_extraction_single_provider_keeps_original_failure():
    only = Provider("cloudflare", error=AIExtractionError(
        "bad json", reason="invalid_provider_output"
    ))
    with pytest.raises(AIExtractionError, match="bad json"):
        manager(only).extract("some cargo ad", "transferlist")
    assert only.calls == 1
