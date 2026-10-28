"""Regression tests for malformed extraction output across AI providers."""

import pytest

import config
from ai.extractor import AIExtractionError
from ai.provider_manager import AIProviderManager
from ai.provider_cooldown import CooldownState


class StubProvider:
    def __init__(self, name, responses):
        self.name = name
        self.responses = list(responses)
        self.calls = 0

    def extract(self, text, category):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def invalid_output():
    return AIExtractionError("invalid JSON", reason="invalid_provider_output", status=400)


def manager_with(primary, alternate):
    manager = AIProviderManager(provider=primary)
    manager.providers = [primary, alternate]
    manager._cooldowns = CooldownState(2)
    manager._unavailable_until = manager._cooldowns.unavailable_until
    return manager


def test_invalid_primary_successful_alternate_does_not_repeat_primary(monkeypatch):
    monkeypatch.setattr(config, "AI_RETRY_COUNT", 0)
    expected = {"category": "transferlist", "data": {"transferlist": {"title": "Cargo"}}}
    primary = StubProvider("groq", [invalid_output()])
    alternate = StubProvider("cloudflare", [expected])
    manager = manager_with(primary, alternate)

    assert manager.extract("cargo", "transferlist") == expected
    assert (primary.calls, alternate.calls) == (1, 1)
    assert manager.active_index == 0


def test_both_malformed_fail_without_cooling_providers(monkeypatch):
    monkeypatch.setattr(config, "AI_RETRY_COUNT", 0)
    primary = StubProvider("groq", [invalid_output()])
    alternate = StubProvider("cloudflare", [invalid_output()])
    manager = manager_with(primary, alternate)

    with pytest.raises(AIExtractionError) as captured:
        manager.extract("cargo", "transferlist")

    assert captured.value.reason == "invalid_provider_output"
    assert (primary.calls, alternate.calls) == (1, 1)
    assert manager._unavailable_until == [0.0, 0.0]


def test_malformed_primary_rate_limited_alternate_is_cooled_down(monkeypatch):
    monkeypatch.setattr(config, "AI_RETRY_COUNT", 0)
    primary = StubProvider("groq", [invalid_output(), invalid_output()])
    alternate = StubProvider("cloudflare", [
        AIExtractionError("rate limited", reason="rate_limit", status=429, retry_after=60)
    ])
    manager = manager_with(primary, alternate)

    with pytest.raises(AIExtractionError) as first:
        manager.extract("cargo", "transferlist")
    assert first.value.status == 429
    assert manager._unavailable_until[1] > 0

    # The service layer may retry; do not hit the cooling alternate again.
    with pytest.raises(AIExtractionError) as second:
        manager.extract("cargo", "transferlist")
    assert second.value.reason == "invalid_provider_output"
    assert (primary.calls, alternate.calls) == (2, 1)


def test_invalid_primary_alternate_permission_error_is_preserved(monkeypatch):
    monkeypatch.setattr(config, "AI_RETRY_COUNT", 0)
    primary = StubProvider("groq", [invalid_output()])
    alternate = StubProvider("cloudflare", [
        AIExtractionError("forbidden", reason="permissions_error", status=403, stop_queue=True)
    ])
    manager = manager_with(primary, alternate)

    with pytest.raises(AIExtractionError) as captured:
        manager.extract("cargo", "transferlist")

    assert captured.value.status == 403
    assert captured.value.stop_queue is True
    assert (primary.calls, alternate.calls) == (1, 1)
