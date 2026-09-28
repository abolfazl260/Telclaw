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
    with pytest.raises(RuntimeError, match="CLOUDFLARE_ACCOUNT_ID"):
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
