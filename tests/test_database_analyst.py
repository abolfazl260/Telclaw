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
    assert result['evidence'][1]['row_count'] == 1
    assert result['evidence'][2]['local_id'] == 1
    assert 'Developer Toronto' in result['evidence'][2]['data']
    assert 'Developer Toronto' in model.messages[-1]
    assert 'Jobs را بررسی کن' in model.messages[-1]


def test_destructive_model_action_rejected_without_changes(analysis_db):
    model = ScriptedModel([{'action':'query','sql':'DELETE FROM messages'},
                           {'action':'answer','answer':'درخواست مجاز نبود.'}])
    result = DatabaseAnalyst(lambda:model).analyze('بررسی کن')
    assert result['evidence'][-1] == {'action':'rejected'}
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
    result = analyst.analyze('ok')
    assert result['partial'] is True
    assert result['evidence'][1]['data']['rows'] == [[1]]
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


def test_final_answer_reserve_is_used_before_budget_failure(analysis_db):
    class ReserveModel(ScriptedModel):
        def should_finish(self):
            return len(self.messages) >= 1
    model = ReserveModel([{'action':'query','sql':'SELECT id FROM messages'},
                          {'action':'answer','answer':'جمع‌بندی بر اساس رکورد ۱'}])
    result = DatabaseAnalyst(lambda:model).analyze('بررسی')
    assert not result['partial']
    assert 'FINAL_ANSWER' in model.messages[-1]
    assert len(result['evidence']) == 2


def test_failed_synthesis_preserves_real_evidence(analysis_db):
    class FailedModel(ScriptedModel):
        def complete(self, messages):
            if self.messages:
                raise AnalysisError('Analysis time limit reached.')
            return super().complete(messages)
    model = FailedModel([{'action':'query','sql':'SELECT id,raw_text FROM messages'}])
    result = DatabaseAnalyst(lambda:model).analyze('بررسی')
    assert result['partial']
    assert 'time limit' in result['warning']
    assert result['evidence'][1]['data']['rows'] == [[1, 'Developer Toronto']]
    assert 'کامل نشد' in result['answer']


def test_planning_failure_can_still_synthesize(analysis_db):
    class RecoverModel(ScriptedModel):
        count = 0
        def complete(self, messages):
            self.count += 1
            if self.count == 1:
                raise AnalysisError('HTTP attempt limit reached.')
            assert messages[-1]['content'].startswith('FINAL_ANSWER')
            return {'action':'answer','answer':'فقط آمار اولیه موجود است؛ علت قطعی مشخص نیست.'}
    result = DatabaseAnalyst(lambda: RecoverModel([])).analyze('بررسی')
    assert not result['partial']
    assert result['evidence'][0]['action'] == 'pipeline_summary'


def test_model_reserves_two_attempts_and_reports_safe_diagnostics(monkeypatch):
    import ai.database_analyst as module
    monkeypatch.setattr(config,'AI_PROVIDERS',('cloudflare','groq'))
    monkeypatch.setattr(config,'CLOUDFLARE_PROVIDERS',[
        {'account_id':'acc','api_token':'SECRET','model':'test'}] * 3)
    monkeypatch.setattr(config,'GROQ_PROVIDERS',[
        {'api_key':'SECRET','model':'test'}] * 3)
    monkeypatch.setattr(module.RateLimiter,'wait',lambda self:None)
    module._LIMITERS.clear()
    monkeypatch.setattr(module.requests,'post',lambda *a,**kw: SimpleNamespace(ok=False,status_code=429))
    model = AnalysisModel()
    with pytest.raises(AnalysisError, match='attempt limit'):
        model.complete([{'role':'user','content':'test'}])
    assert model.calls == 4
    assert [d['provider'] for d in model.diagnostics] == ['cloudflare','groq'] * 2
    assert model.should_finish()
    with pytest.raises(AnalysisError):
        model.complete([{'role':'system','content':'FINAL_ANSWER: summarize'}])
    assert model.calls == 6
    assert all(d['reason']=='http_429' for d in model.diagnostics)
    assert 'SECRET' not in json.dumps(model.diagnostics)


def test_time_limit_has_distinct_reason(monkeypatch):
    import ai.database_analyst as module
    monkeypatch.setattr(config,'AI_PROVIDERS',('groq',))
    monkeypatch.setattr(config,'GROQ_PROVIDERS',[{'api_key':'secret','model':'test'}])
    model=AnalysisModel()
    model.deadline=module.time.monotonic()-1
    with pytest.raises(AnalysisError,match='time limit'):
        model.complete([{'role':'user','content':'test'}])
    assert model.calls == 0
