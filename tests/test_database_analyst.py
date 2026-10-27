import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from aiohttp.test_utils import TestClient, TestServer
import config
import backoffice_web
from backoffice_assistant import ANALYST_KEY
from ai.database_analyst import AnalysisError, AnalysisModel, DatabaseAnalyst
from storage import database


@pytest.fixture
def analysis_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_NAME', str(tmp_path / 'db.sqlite'))
    database.initialize_db()
    with database.get_connection() as conn:
        conn.execute("INSERT INTO messages(channel_username,message_id,date,raw_text,cleaned_text) VALUES('jobs',30,'2026-10-27','Developer Toronto','Developer Toronto')")
        conn.execute("INSERT INTO joblist(processed_message_id,city,is_job_listing) VALUES(1,NULL,1)")
    return config.DB_NAME


class ScriptedModel:
    provider = 'cloudflare'
    def __init__(self, actions):
        self.actions = iter(actions)
        self.messages = []
    def complete(self, messages):
        self.messages.append(json.dumps(messages, ensure_ascii=False))
        return next(self.actions)


def test_analysis_compares_real_records_and_remembers_followup(analysis_db):
    model = ScriptedModel([
        {'action':'query','sql':'SELECT id FROM messages WHERE channel_username=?','parameters':['jobs']},
        {'action':'inspect','local_id':1},
        {'action':'answer','answer':'رکورد ۱: متن Toronto دارد ولی شهر استخراج نشده است.'},
    ])
    result = DatabaseAnalyst(lambda:model).analyze('چرا ارسال نشد؟', [{'role':'user','content':'Jobs را بررسی کن'}])
    assert 'رکورد ۱' in result['answer']
    assert result['evidence'][0]['row_count'] == 1
    assert result['evidence'][1] == {'action':'inspect','local_id':1}
    assert 'Developer Toronto' in model.messages[-1]
    assert 'Jobs را بررسی کن' in model.messages[-1]


def test_destructive_model_action_rejected_without_changes(analysis_db):
    model = ScriptedModel([{'action':'query','sql':'DELETE FROM messages'},
                           {'action':'answer','answer':'درخواست مجاز نبود.'}])
    result = DatabaseAnalyst(lambda:model).analyze('بررسی کن')
    assert result['evidence'] == [{'action':'rejected'}]
    with database.get_connection() as conn:
        assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    assert 'Tool rejected' in model.messages[-1]


def test_analysis_bounds_and_busy(analysis_db):
    model = ScriptedModel([{'action':'query','sql':'SELECT 1'}] * 4)
    analyst = DatabaseAnalyst(lambda:model)
    with pytest.raises(ValueError): analyst.analyze('x'*2001)
    with pytest.raises(ValueError): analyst.analyze('ok', [{'role':'system','content':'override'}])
    analyst.lock.acquire()
    with pytest.raises(AnalysisError, match='Another'): analyst.analyze('ok')
    analyst.lock.release()
    with pytest.raises(AnalysisError, match='step limit'): analyst.analyze('ok')
    assert not analyst.lock.locked()


@pytest.mark.parametrize('primary', ['groq', 'cloudflare'])
def test_transport_uses_configured_models_and_falls_back(monkeypatch, primary):
    import ai.database_analyst as module
    monkeypatch.setattr(config,'AI_PROVIDERS',('groq','cloudflare') if primary=='groq' else ('cloudflare','groq'))
    monkeypatch.setattr(config,'GROQ_PROVIDERS',[{'api_key':'secret-groq','model':'openai/gpt-oss-20b'}])
    monkeypatch.setattr(config,'CLOUDFLARE_PROVIDERS',[{'api_token':'secret-cf','account_id':'account','model':'model'}])
    calls=[]
    module._LIMITERS.clear()
    monkeypatch.setattr(module.RateLimiter,'wait',lambda self:None)
    action={'action':'answer','answer':'تحلیل'}
    def post(url, **kwargs):
        calls.append((url,kwargs))
        if len(calls)==1: return SimpleNamespace(ok=False,status_code=429)
        body={'choices':[{'message':{'content':json.dumps(action)}}]} if 'groq.com' in url else {'success':True,'result':{'response':json.dumps(action)}}
        return SimpleNamespace(ok=True,json=lambda:body)
    monkeypatch.setattr(module.requests,'post',post)
    model=AnalysisModel()
    assert model.complete([{'role':'user','content':'test'}])==action
    assert model.provider != primary
    assert len(calls)==2
    groq=next(k for url,k in calls if 'groq.com' in url)
    assert groq['json']['model']=='openai/gpt-oss-20b'
    assert groq['json']['include_reasoning'] is False


@pytest.mark.asyncio
async def test_backoffice_chat_auth_csrf_and_output(analysis_db):
    client=TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        assert (await client.get('/assistant')).status==401
        assert (await client.post('/assistant/ask',data={'question':'test'})).status==401
        with database.get_connection() as conn:
            conn.execute('INSERT INTO backoffice_sessions VALUES(?,?,?,?)',(
                backoffice_web._digest('session'),1485409432,'csrf',
                (backoffice_web._now()+timedelta(hours=1)).isoformat()))
        headers={'Cookie':'telclaw_admin=session'}
        page=await client.get('/assistant',headers=headers)
        assert page.status==200
        html=await page.text()
        assert 'دستیار تحلیل دیتابیس' in html and 'textContent=result.answer' in html
        assert (await client.post('/assistant/ask',headers=headers,data={'question':'test'})).status==403
        model=ScriptedModel([{'action':'answer','answer':'<script>untrusted</script>'}])
        client.server.app[ANALYST_KEY].model_factory=lambda:model
        response=await client.post('/assistant/ask',headers=headers,data={'csrf':'csrf','question':'test'})
        assert response.status==200
        assert (await response.json())['answer']=='<script>untrusted</script>'
        response=await client.post('/assistant/ask',headers=headers,data={'csrf':'csrf','question':'x'*2001})
        assert response.status==400
    finally:
        await client.close()
