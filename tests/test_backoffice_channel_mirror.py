import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telethon import types

import config
import backoffice_web
from backoffice_channel_mirror import Administration, install
from channel_mirror import Repository, SourceItem, SourcePost
from services.channel_service import ChannelService
from services.crawl_job_service import CrawlJobService

PEER, DEST = -1000000000777, -1009999999999
COOKIE = {'telclaw_admin': 'cm-session'}


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_NAME', str(tmp_path / 'cm.sqlite'))
    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', '123456:super-secret-token')
    monkeypatch.setattr(backoffice_web, 'ADMIN_USER_IDS', {42})
    repo = Repository()
    repo.initialize()
    backoffice_web.initialize_auth()
    with repo.connection(True) as c:
        c.execute('INSERT INTO backoffice_sessions VALUES(?,?,?,?)', (backoffice_web._digest('cm-session'), 42, 'csrf', (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()))
    path = tmp_path / 'channels.json'
    path.write_text('{"Sources": []}')
    job = CrawlJobService(mirror_repository=repo)
    operations = SimpleNamespace(console_ui=None, _client=None, channels=ChannelService(path), crawler=SimpleNamespace(scheduler=SimpleNamespace(crawl_job=job)))
    admin = Administration(repo, operations)
    admin.validate = AsyncMock(return_value=(PEER, DEST))
    return admin


def app(admin):
    result = web.Application(middlewares=[backoffice_web._security])
    install(result, admin)
    return result


def rule(admin):
    return admin.repo.create_rule(PEER, '@source', DEST, '@destination', 'fixed', 42, now=datetime.now(timezone.utc)-timedelta(hours=1))


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/channel-mirror', '/channel-mirror?offset=50'])
async def test_authentication_and_headers(admin, path):
    async with TestClient(TestServer(app(admin))) as client:
        response = await client.get(path)
        assert response.status == 401
        response = await client.get(path, cookies=COOKIE)
        assert response.status == 200 and response.headers['Cache-Control'] == 'no-store'
        assert 'frame-ancestors' in response.headers['Content-Security-Policy']


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['save', 'preview', 'state', 'switch', 'enroll', 'check', 'review'])
async def test_all_mutations_require_csrf(admin, action):
    async with TestClient(TestServer(app(admin))) as client:
        assert (await client.post('/channel-mirror/'+action, cookies=COOKIE, data={})).status == 403
    assert not admin.repo.list_rules()


@pytest.mark.asyncio
async def test_preview_exact_literal_caption_and_limits(admin):
    async with TestClient(TestServer(app(admin))) as client:
        r = await client.post('/channel-mirror/preview', cookies=COOKIE, data={'csrf':'csrf','username':'@destination','footer':'<script>hi</script> & 😀'})
        assert r.status == 200 and r.content_type == 'text/plain'
        assert await r.text() == '@destination\n\n<script>hi</script> & 😀'
        for username, footer in [('123', ''), ('@destination', '😀'*510)]:
            r = await client.post('/channel-mirror/preview', cookies=COOKIE, data={'csrf':'csrf','username':username,'footer':footer})
            assert r.status == 400
    assert not admin.repo.list_rules()


@pytest.mark.asyncio
async def test_save_disabled_activate_confirm_and_enroll_without_starting_timer(admin):
    async with TestClient(TestServer(app(admin))) as client:
        r = await client.post('/channel-mirror/save', cookies=COOKIE, data={'csrf':'csrf','source':'@source','destination':'@destination','username':'@destination','footer':'fixed'}, allow_redirects=False)
        assert r.status == 303
        assert admin.repo.get_rule(1)['state'] == 'disabled'
        r = await client.post('/channel-mirror/state', cookies=COOKIE, data={'csrf':'csrf','id':1,'state':'active'})
        assert r.status == 400 and admin.repo.get_rule(1)['state'] == 'disabled'
        r = await client.post('/channel-mirror/state', cookies=COOKIE, data={'csrf':'csrf','id':1,'state':'active','confirmed':'yes'}, allow_redirects=False)
        assert r.status == 303 and admin.repo.get_rule(1)['activation_watermark']
        assert admin.repo.dashboard()[0]['permission_checked'] != 'Not checked in this panel'
        r = await client.post('/channel-mirror/enroll', cookies=COOKIE, data={'csrf':'csrf','source':'@source','category':'Sources','confirmed':'yes'}, allow_redirects=False)
        assert r.status == 303 and admin.operations.channels.channels_for_category('Sources')[0]['username'] == 'source'
        assert admin.operations.crawler.scheduler.crawl_job.mirror_status()[0]['crawl_status'] == 'waiting_not_scheduled'


@pytest.mark.asyncio
async def test_xss_history_audit_and_secret_redaction(admin):
    rid = rule(admin)
    admin.repo.update_rule(rid, 42, footer='<script>alert(1)</script> '+config.TELEGRAM_BOT_TOKEN)
    async with TestClient(TestServer(app(admin))) as client:
        r = await client.get('/channel-mirror?id=1', cookies=COOKIE)
        text = await r.text()
        assert r.status == 200 and '<script>alert(1)</script>' not in text
        assert '&lt;script&gt;' in text
        assert (await client.get('/channel-mirror?id=bad', cookies=COOKIE)).status == 400
    assert all(config.TELEGRAM_BOT_TOKEN not in a['details'] for a in admin.repo.audit_log())


@pytest.mark.asyncio
async def test_kill_switch_blocks_claim_and_inflight_guard_preserves_queue(admin):
    rid = rule(admin)
    admin.repo.set_rule_state(rid, 'active', 42, now=datetime.now(timezone.utc)-timedelta(minutes=30))
    item = SourcePost(PEER, (SourceItem(10, datetime.now(timezone.utc), 'photo'),), 'run')
    did, = admin.repo.enqueue(item)
    job = admin.repo.claim()
    async with TestClient(TestServer(app(admin))) as client:
        r = await client.post('/channel-mirror/switch', cookies=COOKIE, data={'csrf':'csrf','enabled':'no','confirmed':'yes'}, allow_redirects=False)
        assert r.status == 303
    assert not admin.repo.claim_sendable(job)
    assert not Repository().enabled() and admin.repo.get_delivery(did)['status'] == 'sending'
    assert not admin.repo.has_active_rules()
    old = admin.repo.get_rule(rid)['activation_watermark']
    admin.repo.global_switch(True, 42)
    assert admin.repo.get_rule(rid)['activation_watermark'] > old


@pytest.mark.asyncio
async def test_uncertain_review_is_explicit_and_never_sends(admin):
    from channel_mirror import SendOutcome

    rid = rule(admin)
    admin.repo.set_rule_state(rid, 'active', 42, now=datetime.now(timezone.utc)-timedelta(minutes=30))
    did, = admin.repo.enqueue(SourcePost(PEER, (SourceItem(10, datetime.now(timezone.utc), 'photo'),), 'run'))
    job = admin.repo.claim()
    admin.repo.finish(did, job['claim_token'], SendOutcome('uncertain'))
    async with TestClient(TestServer(app(admin))) as client:
        data = {'csrf':'csrf','delivery_id':did,'decision':'retry','reason':'Checked Telegram','confirmed':'yes'}
        assert (await client.post('/channel-mirror/review', cookies=COOKIE, data=data)).status == 400
        data['risk'] = 'yes'
        r = await client.post('/channel-mirror/review', cookies=COOKIE, data=data, allow_redirects=False)
        assert r.status == 303 and admin.repo.get_delivery(did)['status'] == 'retry'


@pytest.mark.asyncio
async def test_live_validation_rejects_protected_source_and_missing_account(admin, monkeypatch):
    from backoffice_channel_mirror import Administration
    from channel_mirror import ValidationError

    real = Administration(admin.repo, admin.operations)
    with pytest.raises(ValidationError, match='Connect'):
        await real.validate('@source','@destination','@destination','')
    channel = types.Channel(id=777,title='Source',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),noforwards=True)
    class Client:
        async def get_entity(self, _): return channel
    admin.operations._client = Client()
    with pytest.raises(ValidationError, match='non-protected'):
        await real.validate('@source','@destination','@destination','')


def test_enrollment_deduplicates_and_rejects_unknown_category(admin):
    channels = admin.operations.channels
    channels.enroll('Sources', '@source')
    channels.enroll('Sources', 'SOURCE')
    assert len(channels.channels_for_category('Sources')) == 1
    with pytest.raises(ValueError): channels.enroll('Unknown', 'source')


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['switch', 'enroll', 'review'])
async def test_dangerous_actions_require_confirmation(admin, command):
    async with TestClient(TestServer(app(admin))) as client:
        r = await client.post('/channel-mirror/'+command, cookies=COOKIE, data={'csrf':'csrf'})
        assert r.status == 400
    assert admin.repo.enabled() and not admin.operations.channels.channels_for_category('Sources')


@pytest.mark.asyncio
async def test_permission_failure_does_not_activate_and_redacts_api_errors(admin):
    rid = rule(admin)
    admin.validate.side_effect = RuntimeError(config.TELEGRAM_BOT_TOKEN)
    async with TestClient(TestServer(app(admin))) as client:
        r = await client.post('/channel-mirror/state', cookies=COOKIE, data={'csrf':'csrf','id':rid,'state':'active','confirmed':'yes'})
        assert r.status == 400 and config.TELEGRAM_BOT_TOKEN not in await r.text()
    assert admin.repo.get_rule(rid)['state'] == 'disabled'


@pytest.mark.asyncio
async def test_independent_destinations_and_mode_rejection(admin):
    rid = rule(admin)
    other = admin.repo.create_rule(PEER,'@source',DEST-1,'@secondchan','',42)
    async with TestClient(TestServer(app(admin))) as client:
        assert (await client.post('/channel-mirror/state', cookies=COOKIE, data={'csrf':'csrf','id':rid,'state':'active','confirmed':'yes'}, allow_redirects=False)).status == 303
        assert admin.repo.get_rule(other)['state'] == 'disabled'
        r = await client.post('/channel-mirror/save', cookies=COOKIE, data={'csrf':'csrf','mode':'text_only'})
        assert r.status == 400
    admin.repo.global_switch(False,42)
    assert admin.operations.crawler.scheduler.crawl_job.mirror_status()[0]['crawl_status'] == 'globally_stopped'
