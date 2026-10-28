"""Regression coverage for cross-stage provider and credential cooldowns."""

import pytest

import config
from ai.extractor import AIExtractionError
from ai.provider_cooldown import CooldownState
from ai.provider_manager import AIProviderManager
from ai.providers.cloudflare import CloudflareProvider
from ai.providers.groq import GroqProvider


class StubProvider:
    def __init__(self, name, *, error=None):
        self.name = name
        self.error = error
        self.calls = []

    def classify_batch(self, messages):
        self.calls.append("classification")
        if self.error:
            raise self.error
        return {int(item["message_id"]): "joblist" for item in messages}

    def extract(self, text, category):
        self.calls.append("extraction")
        if self.error:
            raise self.error
        return {"category": category, "data": {category: {}}}


def _two_managers(monkeypatch, *, groq_error, cloudflare_error=None):
    monkeypatch.setattr(config, "AI_PROVIDERS", ("groq", "cloudflare"))
    monkeypatch.setattr(config, "AI_RECOVERY_INTERVAL_SECONDS", 1)
    clients = []

    def build(name):
        instance = StubProvider(
            name, error=groq_error if name == "groq" else cloudflare_error
        )
        clients.append(instance)
        return instance

    monkeypatch.setattr("ai.provider_manager.create_provider", build)
    return AIProviderManager(), AIProviderManager(), clients


def test_classification_cooldown_is_observed_by_extraction(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: clock[0])
    limited = AIExtractionError(
        "TPM limit", status=429, reason="rate_limit", retry_after=30
    )
    classification, extraction, clients = _two_managers(
        monkeypatch, groq_error=limited
    )
    # A different Groq client in Extraction would otherwise succeed.
    clients[2].error = None

    assert classification.classify_batch(
        [{"message_id": 1, "text": "job"}]
    ) == {1: "joblist"}
    assert [client.calls for client in clients] == [
        ["classification"], [], [], []
    ]

    assert extraction.extract("job", "joblist")["category"] == "joblist"
    assert clients[2].calls == []  # No second Groq request during cooldown.
    assert clients[3].calls == ["extraction"]

    clock[0] = 1031.0
    assert extraction.extract("job", "joblist")["category"] == "joblist"
    assert clients[2].calls == ["extraction"]  # Highest priority recovered.


def test_extraction_cooldown_is_observed_by_classification(monkeypatch):
    clock = [2000.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: clock[0])
    limited = AIExtractionError(
        "TPM limit", status=429, reason="rate_limit", retry_after=45
    )
    extraction, classification, clients = _two_managers(
        monkeypatch, groq_error=limited
    )
    clients[2].error = None

    extraction.extract("job", "joblist")
    classification.classify_batch([{"message_id": 1, "text": "job"}])
    assert clients[0].calls == ["extraction"]
    assert clients[2].calls == []
    assert clients[3].calls == ["classification"]


def test_all_cooling_providers_are_not_contacted_by_another_stage(monkeypatch):
    clock = [3000.0]
    monkeypatch.setattr("ai.provider_manager.time.monotonic", lambda: clock[0])
    groq_limited = AIExtractionError(
        "TPM limit", status=429, reason="rate_limit", retry_after=40
    )
    cf_limited = AIExtractionError(
        "Workers AI limit", status=429, reason="rate_limit", retry_after=15
    )
    first, second, clients = _two_managers(
        monkeypatch, groq_error=groq_limited, cloudflare_error=cf_limited
    )

    with pytest.raises(AIExtractionError) as exc:
        first.classify_batch([{"message_id": 1, "text": "job"}])
    assert exc.value.retry_after == pytest.approx(15)
    with pytest.raises(AIExtractionError) as next_exc:
        second.extract("job", "joblist")
    assert next_exc.value.status == 429
    assert next_exc.value.retry_after == pytest.approx(15)
    assert [c.calls for c in clients] == [["classification"], ["classification"], [], []]


def test_cooldown_deadline_cannot_be_shortened(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ai.provider_cooldown.time.monotonic", lambda: clock[0])
    state = CooldownState(1)
    state.block(0, 120)
    clock[0] = 101.0
    state.block(0, 10)
    assert state.unavailable_until == [220.0]


def test_groq_account_rotation_is_shared_between_stages(monkeypatch):
    clock = [4000.0]
    monkeypatch.setattr("ai.providers.groq.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        config,
        "GROQ_PROVIDERS",
        [{"api_key": "shared-primary-key", "model": "test-model"},
         {"api_key": "shared-second-key", "model": "test-model"}],
    )
    first, second = GroqProvider(), GroqProvider()

    def limited(*args, **kwargs):
        raise AIExtractionError(
            "limited", reason="rate_limit", status=429, retry_after=201
        )

    first.providers[0].extract = limited
    first.providers[1].extract = lambda *args, **kwargs: {"category": "joblist"}
    assert first.extract("job", "joblist") == {"category": "joblist"}

    second.providers[0].extract = lambda *args, **kwargs: pytest.fail(
        "Second stage used a cooling Groq account"
    )
    second.providers[1].extract = lambda *args, **kwargs: {"category": "joblist"}
    assert second.extract("job", "joblist") == {"category": "joblist"}
    assert second.active_index == 1


def test_groq_all_accounts_cooling_returns_429_without_traffic(monkeypatch):
    monkeypatch.setattr(
        config, "GROQ_PROVIDERS",
        [{"api_key": "all-cooling-key", "model": "test-model"}],
    )
    first, second = GroqProvider(), GroqProvider()
    first._cooldowns.block(0, 25)

    second.providers[0].extract = lambda *args, **kwargs: pytest.fail(
        "Groq HTTP request sent during shared cooldown"
    )
    with pytest.raises(AIExtractionError) as raised:
        second.extract("job", "joblist")
    assert raised.value.status == 429
    assert raised.value.retry_after > 0


class _NoWait:
    def wait(self):
        pass

    def slot(self):
        class _Slot:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Slot()


def test_cloudflare_account_rotation_is_shared_between_stages(monkeypatch):
    monkeypatch.setattr(
        config,
        "CLOUDFLARE_PROVIDERS",
        [
            {"account_id": "shared-cf-first", "api_token": "cf-token-a", "model": "model"},
            {"account_id": "shared-cf-second", "api_token": "cf-token-b", "model": "model"},
        ],
    )
    first, second = CloudflareProvider(), CloudflareProvider()
    first.rate_limiter = second.rate_limiter = _NoWait()
    calls = []

    class Reply:
        def __init__(self, account):
            self.ok = account != "shared-cf-first"
            self.status_code = 200 if self.ok else 429
            self.text = "" if self.ok else "rate limit"
            self.headers = {} if self.ok else {"Retry-After": "60"}

        def json(self):
            return {"result": {"response": '{"classifications":[{"message_id":1,"category":"joblist"}]}'}}

    def fake_post(url, *args, **kwargs):
        account = "shared-cf-first" if "/shared-cf-first/" in url else "shared-cf-second"
        calls.append(account)
        return Reply(account)

    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    assert first.classify_batch([{"message_id": 1, "text": "job"}]) == {1: "joblist"}
    assert calls == ["shared-cf-first", "shared-cf-second"]

    assert second.classify_batch([{"message_id": 1, "text": "job"}]) == {1: "joblist"}
    assert calls == ["shared-cf-first", "shared-cf-second", "shared-cf-second"]
