import json
import pytest
import aiohttp
from message_manager.telegram_sender import TelegramSender
from message_manager.validation import content, buttons, ValidationError


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self, **kwargs):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class SessionFactory:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    def __call__(self, **kwargs):
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def job(asset_key=''):
    return {'chat_id':'@firstchannel', 'content':{'text':'<b>Text</b>','parse_mode':'HTML','buttons':[[{'text':'Open','url':'https://t.me/test'}]],'asset_key':asset_key}}


@pytest.mark.parametrize('body,http,status,delay',[
    ({'ok':False,'error_code':429,'parameters':{'retry_after':120}},429,'retry',120),
    ({'ok':False,'error_code':403,'description':'Forbidden'},403,'failed',0),
    ({'ok':False,'error_code':400,'description':'Invalid content'},400,'failed',0),
    ({'ok':False,'error_code':502},502,'retry',0),
    ({'ok':True,'result':{}},200,'uncertain',0),
    ({},502,'uncertain',0),
    (ValueError('bad json'),502,'uncertain',0),
    ([],200,'uncertain',0),
])
@pytest.mark.asyncio
async def test_transport_outcome_classification(mm, body, http, status, delay):
    factory = SessionFactory([Response(body,http)])
    result = await TelegramSender(mm.media,token='test',session_factory=factory).send(job())
    assert result.status == status and result.retry_after == delay
    url, kwargs = factory.calls[0]
    assert url.endswith('/sendMessage')
    assert kwargs['json']['reply_markup']['inline_keyboard'][0][0]['text'] == 'Open'


@pytest.mark.parametrize('exception,status', [(TimeoutError(),'uncertain'),(aiohttp.ClientPayloadError(),'uncertain'),(aiohttp.ClientConnectionError(),'uncertain')])
@pytest.mark.asyncio
async def test_ambiguous_network_errors_never_auto_retry(mm, exception, status):
    result = await TelegramSender(mm.media,token='test',session_factory=SessionFactory([exception])).send(job())
    assert result.status == status


@pytest.mark.asyncio
async def test_success_includes_private_link(mm):
    factory = SessionFactory([Response({'ok':True,'result':{'message_id':99,'chat':{'id':-1001234}}})])
    result = await TelegramSender(mm.media,token='test',session_factory=factory).send(job())
    assert result.message_id == 99 and result.message_link == 'https://t.me/c/1234/99'


@pytest.mark.parametrize('text', ['<script>alert(1)</script>','<a href="javascript:alert(1)">Click</a>', '<b onclick="x">Hello</b>', '<b>bad</i>', '<img src=x>', '<b>unclosed', '<!-- hidden -->Text', '<pre><b>Bad</b></pre>'])
def test_unsafe_or_invalid_html_rejected(text):
    with pytest.raises(ValidationError):
        content(text)


def test_html_preview_and_utf16_limits():
    text, safe = content('<b>سلام</b> &amp; <a href="https://t.me/test?a=1&amp;b=2">link</a>')
    assert text == safe and '&amp;' in safe
    assert content('<pre><code class="language-python">x &lt; 3</code></pre>')
    with pytest.raises(ValidationError):
        content('🙂' * 2049)
    with pytest.raises(ValidationError):
        content('A' * 1025, has_media=True)
    assert content('<raw>', 'plain')[1] == '&lt;raw&gt;'


@pytest.mark.parametrize('value', ['[[{"text":"x","url":"data:x"}]]', '[[{"text":"x","callback_data":"x"}]]', '{}', '[[]]'])
def test_invalid_keyboards(value):
    with pytest.raises(ValidationError):
        buttons(value)


def test_telegram_url_buttons():
    assert buttons([[{'text':'User','url':'tg://user?id=42'}]])
    assert buttons([[{'text':'Channel','url':'tg://resolve?domain=example'}]])


@pytest.mark.asyncio
async def test_connection_establishment_failure_is_safe_to_retry(mm):
    from types import SimpleNamespace
    error = aiohttp.ClientConnectorError(SimpleNamespace(host='host',port=443,ssl=True),OSError('refused'))
    result = await TelegramSender(mm.media,token='test',session_factory=SessionFactory([error])).send(job())
    assert result.status == 'retry'


@pytest.mark.parametrize('membership,allowed', [
    ({'status':'creator'},True),
    ({'status':'administrator','can_post_messages':True},True),
    ({'status':'administrator','can_post_messages':False},False),
    ({'status':'administrator'},False),
    ({'status':'member'},False),
])
@pytest.mark.asyncio
async def test_channel_permission_check(mm,membership,allowed):
    responses = [Response({'ok':True,'result':{'id':123}}),Response({'ok':True,'result':{'type':'channel','title':'Channel'}}),Response({'ok':True,'result':membership})]
    sender = TelegramSender(mm.media,token='test',session_factory=SessionFactory(responses))
    if allowed:
        assert await sender.check_target({'chat_id':'@firstchannel'}) == 'Channel'
    else:
        with pytest.raises(ValidationError):
            await sender.check_target({'chat_id':'@firstchannel'})


@pytest.mark.asyncio
async def test_permission_429_is_safe_and_updates_shared_cooldown(mm):
    from message_manager.telegram_sender import PreflightRetry
    response = Response({'ok':False,'error_code':429,'parameters':{'retry_after':60}},429)
    sender = TelegramSender(mm.media,token='test',session_factory=SessionFactory([response]))
    with pytest.raises(PreflightRetry) as caught:
        await sender.check_target({'chat_id':'@firstchannel'})
    assert caught.value.retry_after == 60
    with mm.repo.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM publishing_targets WHERE rate_limited_until IS NOT NULL').fetchone()[0] == 2


@pytest.mark.asyncio
async def test_send_respects_cooldown_from_other_publishers(mm):
    from message_manager.models import utcnow
    mm.repo.cooldown(60)
    factory = SessionFactory([])
    result = await TelegramSender(mm.media,token='test',session_factory=factory).send(job())
    assert result.status == 'retry' and result.retry_after >= 59
    assert not factory.calls
