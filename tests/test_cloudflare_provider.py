import json

import pytest

from ai.category_schemas import CATEGORY_FIELDS
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
    monkeypatch.setattr("config.CLOUDFLARE_PROVIDERS", [{"account_id": "account", "api_token": "token", "model": "model"}])
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


@pytest.mark.parametrize("category", CATEGORY_FIELDS)
@pytest.mark.parametrize("invalid_data", [None, [], [{}, {}], "not an object", 42, False])
def test_cloudflare_retries_invalid_category_structure(monkeypatch, category, invalid_data):
    import config

    monkeypatch.setattr(config, "CLOUDFLARE_INVALID_JSON_MAX_RETRIES", 1)
    expected = {"description": "Original source information"}
    replies = iter([
        Response(json.dumps({"category": category, "data": {category: invalid_data}})),
        Response(json.dumps({"category": category, "data": {category: expected}})),
    ])
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return next(replies)

    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    result = provider().extract("Original source information", category)
    assert result["data"][category]["description"] == expected["description"]
    assert len(calls) == 2
    assert calls[1]["messages"][:-1] == calls[0]["messages"]
    assert "nested object structure" in calls[1]["messages"][-1]["content"]
    template_message = calls[0]["messages"][-1]["content"]
    template = json.JSONDecoder().raw_decode(template_message[template_message.index("{"):])[0]
    assert template == {"category": category, "data": {
        category: dict.fromkeys(CATEGORY_FIELDS[category]),
    }}


@pytest.mark.parametrize("invalid_result", [
    [],
    {"category": "joblist"},
    {"category": "joblist", "data": None},
    {"category": "joblist", "data": {}},
    {"category": "housinglist", "data": {"housinglist": {}}},
])
def test_cloudflare_retries_missing_containers_and_wrong_category(monkeypatch, invalid_result):
    import config

    monkeypatch.setattr(config, "CLOUDFLARE_INVALID_JSON_MAX_RETRIES", 1)
    replies = iter([
        Response(json.dumps(invalid_result)),
        Response(json.dumps({"category": "joblist", "data": {
            "joblist": {"is_job_listing": False, "job_title": None},
        }})),
    ])
    monkeypatch.setattr("ai.providers.cloudflare.requests.post", lambda *a, **kw: next(replies))
    result = provider().extract("General job board digest", "joblist")
    assert result["data"]["joblist"]["is_job_listing"] is False
    assert result["data"]["joblist"]["job_title"] is None


@pytest.mark.parametrize("retry_limit", [0, 1, 2])
def test_cloudflare_schema_retry_is_bounded_and_redacts_output(monkeypatch, caplog, retry_limit):
    import config

    monkeypatch.setattr(config, "CLOUDFLARE_INVALID_JSON_MAX_RETRIES", retry_limit)
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return Response(json.dumps({"category": "joblist", "data": {
            "joblist": "sensitive model output",
        }}))

    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    with pytest.raises(AIExtractionError) as raised:
        provider().extract("sensitive source text", "joblist")
    assert raised.value.reason == "invalid_provider_output"
    assert raised.value.provider == "cloudflare"
    assert "data.joblist must be an object" in str(raised.value.__cause__)
    assert len(calls) == retry_limit + 1
    assert "sensitive model output" not in caplog.text + str(raised.value)
    assert "sensitive source text" not in caplog.text + str(raised.value)


@pytest.mark.parametrize("retry_limit", [1, 2])
def test_cloudflare_syntax_and_schema_share_one_retry_budget(monkeypatch, retry_limit):
    import config

    monkeypatch.setattr(config, "CLOUDFLARE_INVALID_JSON_MAX_RETRIES", retry_limit)
    replies = iter([
        Response('{"category":"joblist","data":'),
        Response('{"category":"joblist","data":{"joblist":null}}'),
        Response('{"category":"joblist","data":{"joblist":{"is_job_listing":false}}}'),
    ])
    calls = []
    instance = provider()
    limiter_calls = []
    monkeypatch.setattr(instance.rate_limiter, "wait", lambda: limiter_calls.append(1))

    def fake_post(*args, **kwargs):
        calls.append(1)
        return next(replies)

    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    if retry_limit == 1:
        with pytest.raises(AIExtractionError) as raised:
            instance.extract("Job digest", "joblist")
        assert raised.value.reason == "invalid_provider_output"
    else:
        assert instance.extract("Job digest", "joblist")["data"]["joblist"]["is_job_listing"] is False
    assert len(calls) == len(limiter_calls) == retry_limit + 1


def test_cloudflare_singleton_category_array_still_succeeds_without_retry(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return Response(json.dumps({"category": "joblist", "data": {
            "joblist": [{"job_title": "Developer", "skills": ["Python", "SQL"]}],
        }}))

    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    data = provider().extract("Developer wanted", "joblist")["data"]["joblist"]
    assert data["job_title"] == "Developer"
    assert data["skills"] == ["Python", "SQL"]
    assert len(calls) == 1


def test_cloudflare_invalid_structure_exhaustion_uses_manager_fallback(monkeypatch):
    import config

    monkeypatch.setattr(config, "CLOUDFLARE_INVALID_JSON_MAX_RETRIES", 1)
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return Response('{"category":"joblist","data":{"joblist":null}}')

    class Alternate:
        name = "groq"

        def extract(self, text, category):
            assert text == "General digest"
            assert category == "joblist"
            return {"category": category, "data": {category: {"is_job_listing": False}}}

    monkeypatch.setattr("ai.providers.cloudflare.requests.post", fake_post)
    manager = AIProviderManager(provider=provider())
    manager.providers.append(Alternate())
    manager._unavailable_until.append(0.0)
    result = manager.extract("General digest", "joblist")
    assert result["data"]["joblist"]["is_job_listing"] is False
    assert len(calls) == 2


def _three_cloudflare_accounts():
    return CloudflareProvider(providers=[
        dict(account_id=f'account-{i}', api_token=f'test-token-{i}', model='@cf/test/model')
        for i in range(1, 4)
    ], rate_limiter=FakeRateLimiter())


def _limited_cloudflare_response(wait):
    response = Response('')
    response.ok = False
    response.status_code = 429
    response.headers = {'Retry-After': str(wait)}
    return response


@pytest.mark.parametrize('category', CATEGORY_FIELDS)
@pytest.mark.parametrize('retry_budget', [0, 1, 3])
def test_cloudflare_extraction_uses_all_three_accounts_then_wraps(monkeypatch, category, retry_budget):
    import config
    monkeypatch.setattr(config, 'AI_RETRY_COUNT', retry_budget)
    clock = [100.0]
    monkeypatch.setattr('ai.providers.cloudflare.time.monotonic', lambda: clock[0])
    valid = lambda: Response(json.dumps({'category': category, 'data': {category: {'description': 'source facts'}}}))
    replies = iter([_limited_cloudflare_response(10), _limited_cloudflare_response(20), valid(),
                    _limited_cloudflare_response(30), valid()])
    calls = []
    def post(url, **kwargs):
        calls.append(url.split('/accounts/')[1].split('/')[0])
        return next(replies)
    monkeypatch.setattr('ai.providers.cloudflare.requests.post', post)
    provider = _three_cloudflare_accounts()
    assert provider.extract('source facts', category)['category'] == category
    assert calls == ['account-1', 'account-2', 'account-3']
    clock[0] = 111.0  # First recovered, second still cooling, third now limited.
    assert provider.extract('more source facts', category)['category'] == category
    assert calls == ['account-1', 'account-2', 'account-3', 'account-3', 'account-1']


def test_all_cloudflare_accounts_cool_down_until_earliest_recovery(monkeypatch):
    import config
    monkeypatch.setattr(config, 'AI_RETRY_COUNT', 3)
    clock = [100.0]
    monkeypatch.setattr('ai.providers.cloudflare.time.monotonic', lambda: clock[0])
    replies = iter([_limited_cloudflare_response(30), _limited_cloudflare_response(10), _limited_cloudflare_response(60),
                    Response(json.dumps({'category': 'joblist', 'data': {'joblist': {}}}))])
    calls = []
    def post(url, **kwargs):
        calls.append(url.split('/accounts/')[1].split('/')[0]); return next(replies)
    monkeypatch.setattr('ai.providers.cloudflare.requests.post', post)
    provider = _three_cloudflare_accounts()
    manager = AIProviderManager(provider=provider)
    with pytest.raises(AIExtractionError) as raised:
        manager.extract('source facts', 'joblist')
    assert raised.value.status == 429
    assert raised.value.retry_after == pytest.approx(10)
    assert calls == ['account-1', 'account-2', 'account-3']
    with pytest.raises(AIExtractionError):
        manager.extract('source facts', 'joblist')
    assert len(calls) == 3  # Cooling accounts must receive no HTTP calls.
    clock[0] = 111.0
    assert manager.extract('source facts', 'joblist')['category'] == 'joblist'
    assert calls[-1] == 'account-2'
