import json

import pytest

from ai.category_schemas import CATEGORY_FIELDS
from ai.prompt_loader import render_prompt
from ai.structured_output import extraction_response_format
from ai.providers.groq import GroqClient
from ai.groq_token_budget import GroqTokenBudget
from ai.extractor import AIExtractionError
from test_cloudflare_provider import provider, Response, FakeRateLimiter


@pytest.mark.parametrize('category', CATEGORY_FIELDS)
@pytest.mark.parametrize('model', ['openai/gpt-oss-20b', 'openai/gpt-oss-120b'])
def test_strict_schema_is_closed_and_nullable(category, model):
    result = extraction_response_format(model, category)
    schema = result['json_schema']['schema']
    assert result['json_schema']['strict'] is True
    assert schema['properties']['category']['enum'] == [category]
    data = schema['properties']['data']
    fields = data['properties'][category]
    for node in (schema, data, fields):
        assert node['additionalProperties'] is False
        assert node['required'] == list(node['properties'])
    assert set(fields['properties']) == set(CATEGORY_FIELDS[category])
    for value in fields['properties'].values():
        assert {'type': 'null'} in value['anyOf']


def test_unknown_models_keep_json_mode():
    assert extraction_response_format('another-model', 'joblist') == {'type':'json_object'}


def test_unicode_retry_instructs_regeneration_without_repair(monkeypatch):
    calls=[]
    replies=iter([Response('{"description":"bad \\u06"}'), Response(json.dumps({
        'category':'joblist', 'data':{'joblist':{'job_title':'Developer'}}}))])
    def post(*args, **kwargs):
        calls.append(kwargs['json'])
        return next(replies)
    monkeypatch.setattr('config.CLOUDFLARE_INVALID_JSON_MAX_RETRIES', 1)
    monkeypatch.setattr('ai.providers.cloudflare.requests.post', post)
    assert provider().extract('برنامه نویس', 'joblist')['category'] == 'joblist'
    assert len(calls) == 2
    assert calls[1]['messages'][:-1] == calls[0]['messages']
    assert 'Regenerate the complete' in calls[1]['messages'][-1]['content']
    assert 'bad \\u06' not in calls[1]['messages'][-1]['content']


@pytest.mark.parametrize('finish', ['length', 'stop'])
def test_groq_strict_payload_still_rejects_truncation(monkeypatch, finish):
    calls=[]
    class Reply:
        ok=True
        def json(self):
            return {'choices':[{'finish_reason':finish,'message':{'content':json.dumps({
                'category':'joblist','data':{'joblist':{'job_title':'Developer'}}})}}]}
    def post(*args, **kwargs):
        calls.append(kwargs['json'])
        return Reply()
    monkeypatch.setattr('ai.providers.groq.requests.post', post)
    client=GroqClient(api_key='test',model='openai/gpt-oss-20b',rate_limiter=FakeRateLimiter(), token_budget=GroqTokenBudget(100000))
    if finish == 'length':
        with pytest.raises(AIExtractionError) as exc:
            client.extract('Developer wanted', 'joblist')
        assert exc.value.reason == 'invalid_provider_output'
    else:
        assert client.extract('Developer wanted','joblist')['category'] == 'joblist'
    assert len(calls) == 1
    assert calls[0]['response_format']['json_schema']['strict'] is True
    assert calls[0]['reasoning_effort'] == 'low'


def test_prompt_keeps_source_and_adds_encoding_rules():
    source='استخدام با حقوق ساعتی ۲۰ دلار'
    prompt=render_prompt('joblist',source)
    assert prompt.endswith(source)
    assert 'directly as Unicode characters' in prompt
