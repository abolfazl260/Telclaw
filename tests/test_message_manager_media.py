import io
from pathlib import Path
import pytest
from PIL import Image
from message_manager.validation import ValidationError
from message_manager.telegram_sender import TelegramSender
from test_message_manager_delivery import SessionFactory, Response, job


async def chunks(raw, size=65536):
    for offset in range(0,len(raw),size):
        yield raw[offset:offset+size]


def photo_bytes():
    out = io.BytesIO()
    Image.new('RGB',(100,100),'red').save(out,format='JPEG')
    return out.getvalue()


@pytest.mark.asyncio
async def test_private_generated_upload_hash_and_path_protection(mm):
    asset = await mm.media.upload('../../evil.jpg','image/jpeg',chunks(photo_bytes()),42)
    assert asset['original_name'] == 'evil.jpg'
    assert len(asset['sha256']) == 64 and len(asset['asset_key']) == 32
    assert mm.media.path(asset).name != 'evil.jpg'
    assert mm.media.path(asset).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValidationError):
        mm.media.path({'filename':'../outside.jpg'})
    outside = mm.media.root.parent / 'outside'
    outside.write_text('secret')
    (mm.media.root / 'symlink').symlink_to(outside)
    with pytest.raises(ValidationError):
        mm.media.path({'filename':'symlink'})


@pytest.mark.parametrize('name,mime,raw',[
    ('bad.html','text/html',b'<script>x</script>'),
    ('bad.jpg','image/jpeg',b'fake'),
    ('bad.jpg','application/octet-stream',b'fake'),
    ('bad.mp4','video/mp4',b'fake'),
    ('bad.pdf','application/pdf',b'fake'),
    ('bad.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',b'fake'),
    ('empty.txt','text/plain',b''),
])
@pytest.mark.asyncio
async def test_invalid_uploads_removed(mm,name,mime,raw):
    with pytest.raises(ValidationError):
        await mm.media.upload(name,mime,chunks(raw),42)
    assert not mm.media.root.exists() or not list(mm.media.root.iterdir())


@pytest.mark.asyncio
async def test_bounded_stream_and_unreferenced_retention(mm):
    mm.media.max_bytes = 100
    with pytest.raises(ValidationError,match='exceeds'):
        await mm.media.upload('too-big.txt','text/plain',chunks(b'A'*101),42)
    a = await mm.media.upload('keep.txt','text/plain',chunks(b'keep'),42)
    b = await mm.media.upload('delete.txt','text/plain',chunks(b'delete'),42)
    mm.service.save({'title':'Keep','text':'Body','asset_key':a['asset_key']},42)
    with mm.repo.connection(True) as c:
        c.execute("UPDATE mm_media SET created_at='2020-01-01T00:00:00+00:00'")
    assert mm.media.purge_unreferenced(1) == 1
    assert mm.media.path(a).exists()
    with pytest.raises(ValidationError):
        mm.repo.media(b['asset_key'])


@pytest.mark.parametrize('kind,name,mime,raw',[
    ('photo','test.jpg','image/jpeg',None),
    ('video','test.mp4','video/mp4',b'\x00\x00\x00\x18ftypmp42' + b'0'*20),
    ('document','test.pdf','application/pdf',b'%PDF-1.7\n'),
])
@pytest.mark.asyncio
async def test_media_methods_and_file_id_cache(mm,kind,name,mime,raw):
    asset = await mm.media.upload(name,mime,chunks(raw or photo_bytes()),42)
    media = {'file_id':'telegram-file-id'}
    result_body = {'ok':True,'result':{'message_id':22,'chat':{'username':'firstchannel'},kind:[media] if kind=='photo' else media}}
    factory = SessionFactory([Response(result_body),Response(result_body)])
    sender = TelegramSender(mm.media,token='bot-test',session_factory=factory)
    first = await sender.send(job(asset['asset_key']))
    assert first.status == 'sent' and first.file_id.endswith(':telegram-file-id')
    assert factory.calls[0][0].endswith('/send'+kind.title())
    assert 'data' in factory.calls[0][1]
    with mm.repo.connection(True) as c:
        c.execute('UPDATE mm_media SET file_id=? WHERE asset_key=?',(first.file_id,asset['asset_key']))
    await sender.send(job(asset['asset_key']))
    assert factory.calls[1][1]['json'][kind] == 'telegram-file-id'
