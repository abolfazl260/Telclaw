import json

from ai.provider_manager import AIProviderManager
from ai.providers.groq import GroqProvider


class FakeRateLimiter:
    def wait(self):
        pass

    def slot(self):
        class Slot:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False
        return Slot()


class Response:
    ok = True
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, content):
        self.content = content

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self.content)}}]}


def test_manager_delegates_classification_to_groq_provider(monkeypatch):
    provider = GroqProvider([{"api_key": "key", "model": "model"}])
    provider.classifiers[0].rate_limiter = FakeRateLimiter()
    monkeypatch.setattr(
        "ai.providers.groq.requests.post",
        lambda *args, **kwargs: Response({"classifications": [{"message_id": 1, "category": "housinglist"}]}),
    )

    assert AIProviderManager(provider=provider).classify_batch([{"message_id": 1, "text": "room"}]) == {1: "housinglist"}


def test_manager_delegates_extraction_to_groq_provider(monkeypatch):
    provider = GroqProvider([{"api_key": "key", "model": "model"}])
    provider.providers[0].rate_limiter = FakeRateLimiter()
    payload = {"category": "joblist", "data": {"joblist": {"title": "Developer", "job_title": "Developer"}}}
    monkeypatch.setattr("ai.providers.groq.requests.post", lambda *args, **kwargs: Response(payload))

    result = AIProviderManager(provider=provider).extract("Developer wanted", "joblist")

    assert result["category"] == "joblist"
    assert result["data"]["joblist"]["job_title"] == "Developer"
