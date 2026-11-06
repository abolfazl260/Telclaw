import io
from datetime import timedelta
import pytest
from aiohttp import web, FormData
from aiohttp.test_utils import TestClient, TestServer
import backoffice_web
import config
from backoffice_message_manager import install
from message_manager.models import utcnow
from test_message_manager_excel import workbook


def app_for(mm, monkeypatch, enabled=True):
    monkeypatch.setattr(config,'MESSAGE_MANAGER_ENABLED',enabled)
    monkeypatch.setattr(backoffice_web,'ADMIN_USER_IDS',{42})
    backoffice_web.initialize_auth()
    with mm.repo.connection(True) as c:
        c.execute('INSERT INTO backoffice_sessions VALUES(?,?,?,?)',
            (backoffice_web._digest('mm-session'),42,'mm-csrf',(utcnow()+timedelta(hours=1)).isoformat()))
    app = web.Application(middlewares=[backoffice_web._security],client_max_size=2*1024*1024)
    install(app,mm)
    return app


COOKIES = {'telclaw_admin':'mm-session'}


@pytest.mark.asyncio
async def test_routes_require_session_csrf_and_feature_flag(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        for path in ['/message-manager','/message-manager/template.xlsx','/message-manager/status.xlsx','/message-manager/media/fake']:
            response = await client.get(path)
            assert response.status == 401
        response = await client.post('/message-manager/save',data={'title':'A','text':'Body'},cookies=COOKIES)
        assert response.status == 403
        response = await client.get('/message-manager',cookies=COOKIES)
        assert response.status == 200 and 'Message Manager' in await response.text()
        response = await client.get('/message-manager/template.xlsx',cookies=COOKIES)
        assert response.status == 200 and (await response.read()).startswith(b'PK')
        assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.asyncio
async def test_feature_disabled_has_no_routes(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch,False))) as client:
        response = await client.get('/message-manager',cookies=COOKIES)
        assert response.status == 404


@pytest.mark.asyncio
async def test_save_preview_publish_confirmation_and_schedule_controls(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        response = await client.post('/message-manager/save',data={'csrf':'mm-csrf','title':'<Title>','text':'<b>سلام</b>'},cookies=COOKIES,allow_redirects=False)
        assert response.status == 303
        mid = mm.repo.list_messages()[0]['id']
        response = await client.get(f'/message-manager?id={mid}',cookies=COOKIES)
        html = await response.text()
        assert '<b>سلام</b>' in html and '&lt;Title&gt;' in html
        assert 'Asia/Tehran' in html and 'resending may create a duplicate' not in html
        data = {'csrf':'mm-csrf','id':str(mid),'targets':'1'}
        response = await client.post('/message-manager/publish',data=data,cookies=COOKIES,allow_redirects=False)
        assert response.status == 400 and not mm.sender.sent
        data['confirmed'] = 'yes'
        response = await client.post('/message-manager/publish',data=data,cookies=COOKIES,allow_redirects=False)
        assert response.status == 303 and not mm.sender.sent
        sid = mm.repo.details(mid)[0][0]['id']
        response = await client.post('/message-manager/schedule-action',data={'csrf':'mm-csrf','id':str(mid),'schedule_id':str(sid),'action':'cancel'},cookies=COOKIES,allow_redirects=False)
        assert response.status == 303 and mm.repo.details(mid)[0][0]['state'] == 'cancelled'
        response = await client.post('/message-manager/preview',data={'csrf':'mm-csrf','title':'Test','text':'<script>bad</script>'},cookies=COOKIES)
        assert response.status == 400
        response = await client.get('/message-manager',cookies=COOKIES,params={'q':'Title','target':'1','schedule':'cancelled','status':'draft'})
        assert response.status == 200


@pytest.mark.asyncio
async def test_stream_upload_above_2mib_stays_private_and_bounded(mm,monkeypatch):
    app = app_for(mm,monkeypatch)
    async with TestClient(TestServer(app)) as client:
        headers = {'X-CSRF-Token':'wrong','X-File-Name':'test.txt','Content-Type':'text/plain'}
        response = await client.post('/message-manager/media',data=b'x',headers=headers,cookies=COOKIES)
        assert response.status == 403
        headers['X-CSRF-Token'] = 'mm-csrf'
        raw = b'x'*(2*1024*1024+1)
        response = await client.post('/message-manager/media',data=raw,headers=headers,cookies=COOKIES)
        assert response.status == 200
        asset = await response.json()
        assert app._client_max_size == 2*1024*1024
        url = '/message-manager/media/'+asset['asset_key']
        response = await client.get(url)
        assert response.status == 401
        response = await client.get(url,cookies=COOKIES)
        assert response.status == 200 and await response.read() == raw
        assert response.headers['Content-Disposition'].startswith('attachment')
        mm.media.max_bytes = 10
        response = await client.post('/message-manager/media',data=b'x'*11,headers=headers,cookies=COOKIES)
        assert response.status == 413


@pytest.mark.asyncio
async def test_xlsx_routes_preview_create_and_activate_separately(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        data = FormData()
        data.add_field('csrf','mm-csrf')
        data.add_field('namespace','route-test')
        data.add_field('workbook',workbook(),filename='messages.xlsx',content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response = await client.post('/message-manager/import',data=data,cookies=COOKIES,allow_redirects=False)
        assert response.status == 303
        location = response.headers['Location']
        response = await client.get(location,cookies=COOKIES)
        assert response.status == 200 and 'campaign-1' in await response.text()
        bid = location.split('=')[1]
        post = {'csrf':'mm-csrf','batch_id':bid,'confirmed':'yes'}
        response = await client.post('/message-manager/import-confirm',data=post,cookies=COOKIES,allow_redirects=False)
        assert response.status == 303 and len(mm.repo.list_messages()) == 1
        assert not mm.repo.details(mm.repo.list_messages()[0]['id'])[0]
        response = await client.post('/message-manager/import-activate',data=post,cookies=COOKIES,allow_redirects=False)
        assert response.status == 303
        assert len(mm.repo.details(mm.repo.list_messages()[0]['id'])[0]) == 1


@pytest.mark.asyncio
async def test_register_channel_does_not_create_ad_rule(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        response = await client.post('/message-manager/target',data={'csrf':'mm-csrf','label':'New','chat_id':'@newchannel'},cookies=COOKIES,allow_redirects=False)
        assert response.status == 303
        target = next(t for t in mm.repo.targets() if t['chat_id']=='@newchannel')
        assert target['mm_eligible'] and not target['enabled']
        with mm.repo.connection() as c:
            assert c.execute('SELECT COUNT(*) FROM publishing_rules').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_tabs_separate_editor_channels_imports_and_empty_state(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        response = await client.get('/message-manager',cookies=COOKIES)
        html = await response.text()
        assert 'Create your first message' in html and 'Next 100</a>' not in html
        assert 'id="message-editor"' not in html
        for view, present, absent in [('editor','id="message-editor"','name="namespace"'),('channels','Register a new channel','id="message-editor"'),('imports','name="namespace"','id="message-editor"')]:
            response = await client.get('/message-manager?view='+view,cookies=COOKIES)
            html = await response.text()
            assert response.status == 200 and present in html and absent not in html
        response = await client.get('/message-manager?view=editor',cookies=COOKIES)
        html = await response.text()
        assert 'id="button-builder"' in html and 'id="live-preview"' in html
        assert 'type="hidden" id="asset-key"' in html


@pytest.mark.asyncio
async def test_live_preview_uses_validated_content_and_never_saves(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        data = {'csrf':'mm-csrf','title':'Test','text':'<b>سلام</b>','fragment':'yes','buttons':'[[{"text":"Open","url":"https://example.com"}]]'}
        response = await client.post('/message-manager/preview',data=data,cookies=COOKIES)
        assert response.status == 200
        payload = await response.json()
        assert '<b>سلام</b>' in payload['html'] and 'https://example.com' in payload['html']
        assert mm.repo.list_messages() == [] and not mm.sender.sent
        data['text'] = '<img src=x onerror=alert(1)>'
        response = await client.post('/message-manager/preview',data=data,cookies=COOKIES)
        assert response.status == 400
        data['csrf'] = 'wrong'
        response = await client.post('/message-manager/preview',data=data,cookies=COOKIES)
        assert response.status == 403


@pytest.mark.asyncio
async def test_manual_preview_marks_saved_editor_as_unsaved(mm,monkeypatch):
    mid = mm.service.save({'title':'Original','text':'Saved text'},42)
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        response = await client.post('/message-manager/preview',data={'csrf':'mm-csrf','id':str(mid),'title':'Original','text':'Edited preview'},cookies=COOKIES)
        html = await response.text()
        assert response.status == 200 and 'data-unsaved="true"' in html
        assert mm.repo.get_message(mid)['content']['text'] == 'Saved text'


@pytest.mark.asyncio
async def test_form_errors_keep_input_and_success_has_notice(mm,monkeypatch):
    async with TestClient(TestServer(app_for(mm,monkeypatch))) as client:
        response = await client.post('/message-manager/save',data={'csrf':'mm-csrf','title':'Keep title','text':'<script>bad</script>'},cookies=COOKIES)
        html = await response.text()
        assert response.status == 400 and 'id="message-editor"' in html
        assert 'Keep title' in html and '&lt;script&gt;bad&lt;/script&gt;' in html
        assert 'data-feedback=' in html and mm.repo.list_messages() == []
        response = await client.post('/message-manager/save',data={'csrf':'mm-csrf','title':'Valid','text':'Hello'},cookies=COOKIES)
        assert 'Draft saved successfully.' in await response.text()


def test_dashboard_reports_actual_counts_and_worker_expiry(mm):
    from datetime import timedelta
    from test_message_manager_repository import scheduled, NOW
    from message_manager.models import SendResult
    scheduled(mm,(1,))
    mm.repo.materialize(NOW)
    job = mm.repo.claim(NOW)
    mm.repo.finish(job,SendResult('sent',message_id=12),now=NOW)
    status = mm.repo.dashboard(NOW)
    assert status['sent'] == 1 and status['upcoming'] == 0
    assert status['worker_state'] == 'unknown'
    mm.repo.worker_heartbeat()
    assert mm.repo.dashboard()['worker_state'] == 'running'
    assert mm.repo.dashboard(utcnow()+timedelta(hours=1))['worker_state'] == 'stale'
    mm.repo.cooldown(60)
    assert mm.repo.dashboard()['cooldown']
