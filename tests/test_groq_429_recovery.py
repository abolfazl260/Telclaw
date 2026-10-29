"""Regression tests for Groq TPM 429 feedback and alert classification."""

import json
import logging

import pytest

from ai.extractor import AIExtractionError
from ai.groq_token_budget import GroqTokenBudget
from ai.providers.groq import GroqClient, _GroqBatchClassifier


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


def request():
    return {
        "messages": [{"role": "user", "content": "Tehran to Toronto"}],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 120,
    }


TPM_MESSAGE = (
    "Rate limit reached for model `openai/gpt-oss-20b` in organization "
    "`org_test` service tier `on_demand` on tokens per minute (TPM): "
    "Limit 8000, Used 6027, Requested 2609. Please try again in 4.77s."
)


class Denied:
    ok = False
    status_code = 429
    text = json.dumps({"error": {"message": TPM_MESSAGE, "type": "tokens",
                                 "code": "rate_limit_exceeded"}})

    def __init__(self, headers=None):
        self.headers = headers if headers is not None else {"Retry-After": "5"}


def test_groq_tpm_diagnostics_calibrate_budget_and_honor_retry_after(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ai.groq_token_budget.time.monotonic", lambda: clock[0])
    budget = GroqTokenBudget(8000, 10)
    estimated = budget.reserve(request())
    assert estimated > 0
    budget.complete(estimated, response=Denied())

    snapshot = budget.snapshot()
    assert snapshot["safe_tpm"] == 7200
    assert snapshot["available_estimate"] <= 1173  # 8000 - 6027 - 800 headroom
    assert snapshot["estimate_multiplier"] > 1
    assert snapshot["cooldown_seconds"] == pytest.approx(5)

    with pytest.raises(AIExtractionError) as delayed:
        budget.reserve(request())
    assert delayed.value.status == 429
    assert delayed.value.retry_after == pytest.approx(5)
    clock[0] += 6
    budget.reserve(request())


def test_groq_tpm_body_is_used_when_retry_header_is_missing(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ai.groq_token_budget.time.monotonic", lambda: clock[0])
    budget = GroqTokenBudget(8000, 10)
    reserved = budget.reserve(request())
    budget.complete(reserved, response=Denied({}))
    assert budget.snapshot()["cooldown_seconds"] == pytest.approx(4.77)


@pytest.mark.parametrize("body", [
    "",
    "not json",
    json.dumps({"error": {"message": "Rate limit on requests per minute (RPM)"}}),
    json.dumps({"error": {"message": "tokens per day (TPD): Limit 8000, Used 1000, Requested 4000"}}),
])
def test_non_tpm_errors_do_not_train_token_estimator(body):
    budget = GroqTokenBudget(8000, 10)
    reserved = budget.reserve(request())
    response = Denied({})
    response.text = body
    budget.complete(reserved, response=response)
    assert budget.snapshot()["estimate_multiplier"] == 1.0


@pytest.mark.parametrize("stage", ["extraction", "classification"])
def test_groq_429_warning_not_telegram_error(monkeypatch, caplog, stage):
    budget = GroqTokenBudget(100000)
    if stage == "extraction":
        client = GroqClient(api_key="key", model="test-model",
                            rate_limiter=NoWait(), token_budget=budget)
        call = lambda: client.extract("Need package delivery", "transferlist")
    else:
        client = _GroqBatchClassifier(api_key="key", model="test-model",
                                      rate_limiter=NoWait(), token_budget=budget)
        call = lambda: client.classify_batch([{"message_id": 1, "text": "Delivery"}])

    monkeypatch.setattr("ai.providers.groq.requests.post", lambda *args, **kwargs: Denied())
    with caplog.at_level(logging.WARNING, logger="telclaw.ai"):
        with pytest.raises(AIExtractionError) as raised:
            call()
    assert raised.value.status == 429
    assert raised.value.retry_after == pytest.approx(5)
    messages = [record for record in caplog.records
                if record.name == "telclaw.ai" and "Groq" in record.message]
    assert messages
    assert all(record.levelno == logging.WARNING for record in messages)


def test_groq_permanent_http_error_remains_error(monkeypatch, caplog):
    budget = GroqTokenBudget(100000)
    client = GroqClient(api_key="key", model="test-model",
                        rate_limiter=NoWait(), token_budget=budget)
    response = Denied({})
    response.ok = False
    response.status_code = 401
    response.text = '{"error":{"message":"invalid api key"}}'
    monkeypatch.setattr("ai.providers.groq.requests.post", lambda *args, **kwargs: response)
    with caplog.at_level(logging.ERROR, logger="telclaw.ai"):
        with pytest.raises(AIExtractionError) as raised:
            client.extract("Need package delivery", "transferlist")
    assert raised.value.status == 401
    assert any(record.levelno == logging.ERROR and "Groq provider error" in record.message
               for record in caplog.records)
