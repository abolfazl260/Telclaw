"""Groq organization TPM admission and accounting regression tests."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import config
from ai.extractor import AIExtractionError
from ai.groq_token_budget import GroqTokenBudget, shared_groq_token_budget
from ai.provider_manager import AIProviderManager
from ai.providers.groq import GroqClient, GroqProvider, _GroqBatchClassifier


def payload(text="an advertisement", maximum_output=120):
    return {
        "messages": [{"role": "system", "content": text}],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": maximum_output,
    }


def test_tpm_preflight_blocks_without_spending_tokens(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ai.groq_token_budget.time.monotonic", lambda: clock[0])
    budget = GroqTokenBudget(1000, headroom_percent=10)
    request = payload("a" * 400, maximum_output=250)
    required = budget.estimate_tokens(request)
    budget.available = required - 5

    with pytest.raises(AIExtractionError) as raised:
        budget.reserve(request)
    assert raised.value.status == 429
    assert raised.value.reason == "rate_limit"
    assert 0 < raised.value.retry_after < 60
    assert budget.available == required - 5

    clock[0] += 1
    assert budget.reserve(request) == required


def test_tpm_oversized_request_rejected_even_with_full_bucket():
    budget = GroqTokenBudget(1000)
    with pytest.raises(AIExtractionError, match="exceeds"):
        budget.reserve(payload("x" * 6000, maximum_output=500))
    assert budget.available == budget.capacity


def test_tpm_refills_and_uses_real_usage(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ai.groq_token_budget.time.monotonic", lambda: clock[0])
    budget = GroqTokenBudget(3000, 0)
    requested = budget.reserve(payload("short", maximum_output=600))
    assert budget.available == 3000 - requested
    budget.reconcile_usage(requested, {"usage": {"total_tokens": 200}})
    assert budget.available == 2800
    clock[0] += 10
    budget.reserve(payload("second", maximum_output=100))
    assert budget.available > 2800


def test_tpm_cached_prompt_tokens_not_charged_twice():
    budget = GroqTokenBudget(2000, 0)
    requested = budget.reserve(payload(maximum_output=500))
    budget.reconcile_usage(
        requested,
        {"usage": {"total_tokens": 900,
                   "prompt_tokens_details": {"cached_tokens": 300}}},
    )
    assert budget.available == pytest.approx(1400, abs=0.1)


def test_provider_headers_reduce_local_tpm_and_block_after_429(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ai.groq_token_budget.time.monotonic", lambda: clock[0])
    budget = GroqTokenBudget(8000, 10)
    requested = budget.reserve(payload(maximum_output=1000))

    class Response:
        status_code = 429
        headers = {
            "Retry-After": "15",
            "x-ratelimit-limit-tokens": "4000",
            "x-ratelimit-remaining-tokens": "2000",
            "x-ratelimit-reset-tokens": "7.66s",
        }

    budget.complete(requested, response=Response())
    assert budget.capacity == 3600
    assert budget.available <= 1600

    with pytest.raises(AIExtractionError) as waiting:
        budget.reserve(payload(maximum_output=100))
    assert waiting.value.retry_after == pytest.approx(15)

    clock[0] += 16
    budget.reserve(payload(maximum_output=100))


def test_in_flight_reservations_are_atomic():
    budget = GroqTokenBudget(1500, 0)
    request = payload("b" * 500, maximum_output=700)
    required = budget.estimate_tokens(request)
    assert 750 < required < 1500
    gate = threading.Barrier(2)

    def attempt():
        gate.wait(timeout=2)
        try:
            return budget.reserve(request)
        except AIExtractionError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(attempt) for _ in range(2)]]
    assert sorted(results) == sorted([required, 429])


def test_registry_is_shared_across_groq_keys_and_stages():
    first = shared_groq_token_budget(4021, 10)
    second = shared_groq_token_budget(4021, 10)
    assert first is second
    assert first is not shared_groq_token_budget(4022, 10)


class NoWait:
    def wait(self):
        pass

    def slot(self):
        class Slot:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Slot()


def test_classification_usage_exhaustion_blocks_extraction_before_http(monkeypatch):
    budget = GroqTokenBudget(4000, 0)
    classifier = _GroqBatchClassifier(
        api_key="key-one", model="test-model",
        rate_limiter=NoWait(), token_budget=budget,
    )
    extractor = GroqClient(
        api_key="key-two", model="test-model",
        rate_limiter=NoWait(), token_budget=budget,
    )
    calls = []

    class Result:
        ok = True
        status_code = 200
        headers = {}

        def json(self):
            return {
                "usage": {"total_tokens": 3900},
                "choices": [{"message": {"content": '{"classifications":[{"message_id":1,"category":"joblist"}]}'}}],
            }

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return Result()

    monkeypatch.setattr("ai.providers.groq.requests.post", fake_post)
    assert classifier.classify_batch([{"message_id": 1, "text": "Developer wanted"}]) == {1: "joblist"}

    with pytest.raises(AIExtractionError) as raised:
        extractor.extract("Developer wanted", "joblist")
    assert raised.value.status == 429
    assert len(calls) == 1


def test_tpm_preflight_uses_existing_cloudflare_failover(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDERS", ("groq", "cloudflare"))
    monkeypatch.setattr(config, "GROQ_PROVIDERS", [
        {"api_key": "tpm-test-key", "model": "test-model"},
    ])
    monkeypatch.setattr(config, "GROQ_TOKENS_PER_MINUTE", 100)
    monkeypatch.setattr(config, "GROQ_TPM_HEADROOM_PERCENT", 10)
    calls = []

    class Cloudflare:
        name = "cloudflare"

        def classify_batch(self, messages):
            calls.append("cloudflare")
            return {1: "joblist"}

    def factory(name):
        return GroqProvider() if name == "groq" else Cloudflare()

    monkeypatch.setattr("ai.provider_manager.create_provider", factory)
    monkeypatch.setattr(
        "ai.providers.groq.requests.post",
        lambda *args, **kwargs: pytest.fail("TPM-denied request reached Groq HTTP"),
    )
    manager = AIProviderManager()
    assert manager.classify_batch([{"message_id": 1, "text": "job"}]) == {1: "joblist"}
    assert calls == ["cloudflare"]
    assert manager.provider == "cloudflare"
