"""Focused regression tests for Back Office AI cooldown diagnostics."""

import json
import time
from types import SimpleNamespace

import pytest

import config
from ai.extractor import AIExtractionError
from ai.groq_token_budget import shared_groq_token_budget
from ai.provider_cooldown import CooldownState, cooldown_cause
from ai.provider_manager import AIProviderManager
from ai.providers.groq import GroqClient, GroqProvider
from backoffice_ai_providers import snapshot
from backoffice_ai_providers_ui import render, _tokens_label


@pytest.fixture
def options(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDERS", ("groq", "cloudflare"))
    monkeypatch.setattr(config, "GROQ_PROVIDERS", [
        {"api_key": "never-leak-a", "model": "openai/gpt-oss-20b"},
        {"api_key": "never-leak-b", "model": "openai/gpt-oss-20b"},
    ])
    monkeypatch.setattr(config, "CLOUDFLARE_PROVIDERS", [
        {"account_id": "private-account", "api_token": "never-leak-c",
         "model": "@cf/meta/llama-3.1-8b-instruct"}
    ])
    monkeypatch.setattr(config, "GROQ_TOKENS_PER_MINUTE", 9871)
    monkeypatch.setattr(config, "GROQ_TPM_HEADROOM_PERCENT", 10)


def _operation(groq_waits=(0, 0), cloudflare_wait=0.0):
    outer = CooldownState(2)
    groq = SimpleNamespace(
        name="groq", providers=[object(), object()],
        _cooldowns=CooldownState(2),
    )
    groq._unavailable_until = groq._cooldowns.unavailable_until
    cloudflare = SimpleNamespace(
        name="cloudflare", providers=[object()], _cooldowns=CooldownState(1),
    )
    cloudflare._unavailable_until = cloudflare._cooldowns.unavailable_until
    for i, wait in enumerate(groq_waits):
        if wait:
            groq._cooldowns.block(i, wait, cause="tpm_limit")
    if cloudflare_wait:
        cloudflare._cooldowns.block(0, cloudflare_wait, cause="network_error")
    manager = SimpleNamespace(
        providers=[groq, cloudflare], active_index=1,
        _cooldowns=outer, _unavailable_until=outer.unavailable_until,
    )
    return SimpleNamespace(_ai=SimpleNamespace(provider_manager=manager),
                           _classification=SimpleNamespace(provider_manager=manager)), manager


def test_existing_manager_is_observed_but_not_selected(options):
    operations, manager = _operation()
    data = snapshot(operations)
    groq, cloudflare = data["providers"]
    assert groq["observed"] is True
    assert groq["selected_for"] == []
    assert groq["selection_label"] == "Not selected"
    assert cloudflare["selection_label"] == "Extraction, Classification"
    assert groq["last_cooldown"] is None
    assert "Not selected" in render(data, "nonce-123")


def test_noninitialized_manager_remains_not_observed(options):
    data = snapshot()
    assert data["providers"][0]["selection_label"] == "Not observed"
    assert data["providers"][0]["tokens"]["usable_now"] is False


def test_tpm_cooldown_preserves_full_local_budget_but_labels_unusable(options):
    local = shared_groq_token_budget(9871, 10)
    operations, manager = _operation(groq_waits=(560, 560))
    data = snapshot(operations)
    groq = data["providers"][0]
    assert groq["state"] == "Cooldown"
    assert 0 < groq["cooldown_seconds"] <= 560
    assert groq["tokens"]["available_estimate"] == groq["tokens"]["safe_tpm"]
    assert groq["tokens"]["usable_now"] is False
    assert "On cooldown — not currently usable" in _tokens_label(groq)
    assert "On cooldown" in render(data, "nonce-123")
    assert local is not None


def test_cooldown_cause_and_utc_timestamp_on_account_and_provider(options):
    operations, manager = _operation(groq_waits=(320, 400))
    manager._cooldowns.block(0, 450, cause="tpm_limit")
    groq = snapshot(operations)["providers"][0]
    assert groq["state"] == "Cooldown"
    assert groq["last_cooldown"]["code"] == "tpm_limit"
    assert groq["last_cooldown"]["at"].endswith("+00:00")
    assert groq["last_cooldown"]["active"]
    assert groq["accounts"][0]["last_cooldown"]["code"] == "tpm_limit"
    assert groq["accounts"][1]["last_cooldown"]["code"] == "tpm_limit"
    assert groq["cooldown_seconds"] > 400
    serialized = json.dumps(groq) + render(snapshot(operations), "nonce-456")
    assert "Groq tokens-per-minute (TPM)" in serialized
    for secret in ("never-leak-a", "never-leak-b", "never-leak-c", "private-account"):
        assert secret not in serialized


def test_cloudflare_cooldown_cause_is_displayed(options):
    operations, manager = _operation(cloudflare_wait=75)
    manager._cooldowns.block(1, 75, cause="network_error")
    cloudflare = snapshot(operations)["providers"][1]
    assert cloudflare["last_cooldown"]["code"] == "network_error"
    assert cloudflare["accounts"][0]["last_cooldown"]["code"] == "network_error"
    assert cloudflare["state"] == "Cooldown"
    assert "Network error" in render(snapshot(operations), "nonce")


def test_groq_account_only_cooldown_shows_remaining_until_earliest_eligible(options):
    operations, manager = _operation(groq_waits=(50, 25))
    groq = snapshot(operations)["providers"][0]
    assert groq["state"] == "Cooldown"
    assert 0 < groq["cooldown_seconds"] <= 25
    assert groq["last_cooldown"]["code"] == "tpm_limit"


def test_unattributed_legacy_cooldown_must_not_claim_prior_reason(options):
    operations, manager = _operation()
    groq = manager.providers[0]
    groq._cooldowns.block(0, 20, cause="tpm_limit")
    groq._cooldowns.unavailable_until[0] = time.monotonic() + 200
    groq._cooldowns._started_at[0] = None
    groq._cooldowns._causes[0] = None
    groq._cooldowns.block(1, 200, cause="rate_limit")
    latest = snapshot(operations)["providers"][0]["last_cooldown"]
    assert latest["code"] == "rate_limit"
    assert latest["active"]


def test_cooldown_deadline_reason_is_not_replaced_by_shorter_event(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ai.provider_cooldown.time.monotonic", lambda: clock[0])
    state = CooldownState(1)
    state.block(0, 300, cause="tpm_limit")
    first = state.snapshot()[0]
    clock[0] = 101.0
    state.block(0, 5, cause="network_error")
    second = state.snapshot()[0]
    assert second == first
    assert second["cause"] == "tpm_limit"


def test_cooldown_reason_whitelist_rejects_secret_values():
    secret = "Authorization: Bearer super-private"
    err = AIExtractionError(secret, reason=secret, cooldown_cause=secret)
    assert cooldown_cause(err) == "unknown"
    state = CooldownState(1)
    state.block(0, 25, cause=secret)
    record = state.snapshot()[0]
    assert record["cause"] == "unknown"
    assert secret not in str(record)


def test_real_groq_429_scope_categorization():
    assert GroqClient._cooldown_cause_for_response(
        429, "on tokens per minute (TPM): Limit 8000, Used 6027, Requested 2609"
    ) == "tpm_limit"
    assert GroqClient._cooldown_cause_for_response(
        429, "on requests per minute (RPM)"
    ) == "rpm_limit"
    assert GroqClient._cooldown_cause_for_response(
        429, "on tokens per day (TPD)"
    ) == "daily_quota"
    assert GroqClient._cooldown_cause_for_response(429, "other 429") == "rate_limit"
    assert GroqClient._cooldown_cause_for_response(403, "tokens per minute (TPM)") is None


def test_groq_credential_rotation_records_cause(monkeypatch, options):
    monkeypatch.setattr(config, "GROQ_FAILOVER_THRESHOLD_SECONDS", 200)
    provider = GroqProvider(providers=config.GROQ_PROVIDERS)
    provider.providers[0].extract = lambda *a, **kw: (_ for _ in ()).throw(
        AIExtractionError("limited", status=429, reason="rate_limit",
                          retry_after=560, cooldown_cause="tpm_limit")
    )
    provider.providers[1].extract = lambda *a, **kw: {"category": "joblist"}
    assert provider.extract("job", "joblist") == {"category": "joblist"}
    assert provider._cooldowns.snapshot()[0]["cause"] == "tpm_limit"


def test_outer_manager_records_cause_on_failover(monkeypatch, options):
    class Stub:
        def __init__(self, name, failure=None):
            self.name = name
            self.failure = failure

        def extract(self, text, category):
            if self.failure:
                raise self.failure
            return {"category": category}

    first = Stub("groq", AIExtractionError(
        "rate limited", status=429, reason="rate_limit", retry_after=50,
        cooldown_cause="tpm_limit",
    ))
    second = Stub("cloudflare")
    manager = AIProviderManager(provider=first)
    manager.providers = [first, second]
    manager._cooldowns = CooldownState(2)
    manager._unavailable_until = manager._cooldowns.unavailable_until
    assert manager.extract("job", "joblist")["category"] == "joblist"
    assert manager._cooldowns.snapshot()[0]["cause"] == "tpm_limit"
