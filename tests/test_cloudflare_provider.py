import json

import pytest

from ai.extractor import AIExtractionError
from ai.provider_manager import AIProviderManager
from ai.providers.cloudflare import CloudflareProvider
from ai.providers.groq import GroqProvider


class FakeRateLimiter:
    def wait(self):
        pass

    def slot(self):
        class Slot:
            def __enter__(self): return self
            def __exit__(self, *args): return False
        return Slot()


class Response:
    ok = True
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, result): self.result = result
    def json(self): return {"success": True, "result": {"response": self.result}}


def provider():
    return CloudflareProvider("account", "token", "@cf/test/model", rate_limiter=FakeRateLimiter())


def test_cloudflare_missing_credentials_fail_clearly():
    with pytest.raises(RuntimeError, match="missing account_id, api_token, model"):
        CloudflareProvider("", "", "")


def test_cloudflare_classification_normalizes_response(monkeypatch):
    monkeypatch.setattr("ai.providers.cloudflare.requests.post", lambda *args, **kwargs: Response(json.dumps({"classifications": [{"message_id": 9, "category": "housinglist"}]})))
    assert provider().classify_batch([{"message_id": 9, "text": "Room for rent"}]) == {9: "housinglist"}


def test_cloudflare_extraction_normalizes_response(monkeypatch):
    result = {"category": "joblist", "data": {"joblist": {"title": "Developer", "job_title": "Developer"}}}
    monkeypatch.setattr("ai.providers.cloudflare.requests.post", lambda *args, **kwargs: Response(json.dumps(result)))
    normalized = provider().extract("Developer wanted", "joblist")
    assert normalized["data"]["joblist"]["job_title"] == "Developer"


def test_cloudflare_invalid_json_is_structured_provider_error(monkeypatch):
    monkeypatch.setattr("ai.providers.cloudflare.requests.post", lambda *args, **kwargs: Response("not json"))
    with pytest.raises(AIExtractionError, match="Invalid Cloudflare JSON output") as raised:
        provider().classify_batch([{"message_id": 9, "text": "Room"}])
    assert raised.value.reason == "invalid_provider_output"


def test_manager_can_instantiate_both_providers(monkeypatch):
    monkeypatch.setattr("config.AI_PROVIDERS", ("groq",))
    monkeypatch.setattr("config.GROQ_PROVIDERS", [{"api_key": "key", "model": "model"}])
    assert isinstance(AIProviderManager().provider_instance, GroqProvider)
    monkeypatch.setattr("config.AI_PROVIDERS", ("cloudflare",))
    monkeypatch.setattr("config.CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setattr("config.CLOUDFLARE_API_TOKEN", "token")
    monkeypatch.setattr("config.CLOUDFLARE_MODEL", "model")
    assert isinstance(AIProviderManager().provider_instance, CloudflareProvider)


def test_cloudflare_retries_truncated_json_then_returns_complete_result(monkeypatch):
    import config
    monkeypatch.setattr(config, "CLOUDFLARE_INVALID_JSON_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "CLOUDFLARE_MAX_TOKENS", 0)
    replies = iter([
        Response('{"category":"joblist","data":{"joblist":{"title":"unclosed'),
        Response(json.dumps({
            "category": "joblist",
            "data": {"joblist": {"title": "Developer", "job_title": "Developer"}},
        })),
    ])
    calls = []
    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return next(replies)
    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    assert provider().extract("Developer wanted", "joblist")["category"] == "joblist"
    assert len(calls) == 2


def test_cloudflare_invalid_json_retry_is_bounded(monkeypatch, caplog):
    import config
    monkeypatch.setattr(config, "CLOUDFLARE_INVALID_JSON_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "CLOUDFLARE_MAX_TOKENS", 0)
    calls = []
    def fake_post(*args, **kwargs):
        calls.append(1)
        return Response('{"description": "sensitive listing text')
    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    with pytest.raises(AIExtractionError) as raised:
        provider().extract("sensitive listing text", "transferlist")
    assert raised.value.reason == "invalid_provider_output"
    assert len(calls) == 2
    assert "sensitive listing text" not in caplog.text


def test_cloudflare_max_tokens_is_only_sent_when_configured(monkeypatch):
    import config
    result = json.dumps({"classifications": [{"message_id": 12, "category": "none"}]})
    calls = []
    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return Response(result)
    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    monkeypatch.setattr(config, "CLOUDFLARE_MAX_TOKENS", 0)
    provider().classify_batch([{"message_id": 12, "text": "hello"}])
    assert "max_tokens" not in calls[-1]
    monkeypatch.setattr(config, "CLOUDFLARE_MAX_TOKENS", 2048)
    provider().classify_batch([{"message_id": 12, "text": "hello"}])
    assert calls[-1]["max_tokens"] == 2048
