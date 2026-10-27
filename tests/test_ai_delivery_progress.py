"""Progress must distinguish AI extraction from actual delivery evidence."""
from types import SimpleNamespace
import pytest
import config
from ai.ai_service import AIProcessingService


@pytest.mark.parametrize('category,listing,label',[
    ('joblist', True, 'verified'), ('joblist', False, 'not_verified'),
    ('joblist', None, 'unknown'), ('transferlist', None, None),
])
def test_independent_queues_do_not_claim_a_send(monkeypatch,capsys,category,listing,label):
    monkeypatch.setattr(config,'ADVERTIO_INGEST_ENABLED',False)
    monkeypatch.setattr(config,'AI_EXTRACTION_ENABLED',True)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED,category,True)
    saved=[]
    repository=SimpleNamespace(mark_ai_processing=lambda *a:True,
        save_category_record=lambda *a:saved.append(a),mark_ai_result=lambda **kw:None)
    provider=SimpleNamespace(provider='cloudflare',extract=lambda text,category:
        {'category':category,'data':{category:{'is_job_listing':listing,'description':text}}})
    # Jobs and Cargo must not invoke the inline housing delivery adapter.
    def forbidden(*a,**kw): raise AssertionError('Inline delivery should not run')
    service=AIProcessingService(repository=repository,provider_manager=provider,
        advertio_service=SimpleNamespace(deliver=forbidden))
    processed,failed,*_=service._process([{'id':1,'message_id':306651,'channel_username':'source',
        'classification_category':category,'cleaned_text':'A source message'}],progress=True)
    output=capsys.readouterr().out
    assert processed==1 and failed==0 and saved
    assert 'status=processed | advertio=not_attempted' in output
    assert 'publication_validation=not_checked' in output
    assert 'delivery_stage='+('jobs_queue' if category=='joblist' else 'cargo_queue') in output
    assert 'advertio=ok' not in output
    if label: assert 'job_listing='+label in output


@pytest.mark.parametrize('delivery,expected',[
    ({'attempted':1,'sent':1},'sent'),
    ({'attempted':1,'already_existed':1},'already_existed'),
    ({'attempted':1,'failed':1},'failed'),
    ({'attempted':0,'skipped':1},'skipped'),
    ({'attempted':0},'not_attempted'),
    ({'attempted':1},'unknown'),
])
def test_inline_delivery_progress_tracks_actual_result(monkeypatch,capsys,delivery,expected):
    monkeypatch.setattr(config,'ADVERTIO_INGEST_ENABLED',False)
    monkeypatch.setattr(config,'AI_EXTRACTION_ENABLED',True)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED,'housinglist',True)
    repository=SimpleNamespace(mark_ai_processing=lambda *a:True,
        save_category_record=lambda *a:None,mark_ai_result=lambda **kw:None)
    provider=SimpleNamespace(provider='mock',extract=lambda text,category:
        {'category':category,'data':{category:{'description':text}}})
    service=AIProcessingService(repository=repository,provider_manager=provider)
    monkeypatch.setattr(service,'_deliver_to_advertio',lambda *a:delivery)
    service._process([{'id':1,'message_id':2,'channel_username':'source',
        'classification_category':'housinglist','cleaned_text':'Original text'}],progress=True)
    output=capsys.readouterr().out
    assert 'advertio='+expected in output
    assert 'advertio=ok' not in output
