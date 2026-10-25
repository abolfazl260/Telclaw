import pytest

import config
from storage import database
from test_jobs_discovery import env as env
from test_advertio_jobs import add_post
from backoffice_operations import BackofficeOperations
from test_backoffice_operations import FakeConsole


def missing(message_id):
    mid=add_post(message_id=message_id)
    with database.get_connection() as conn:
        conn.execute('DELETE FROM joblist WHERE processed_message_id=?',(mid,))
    return mid


def test_only_missing_success_requeued_newest_first_without_touching_source(env):
    missing(1)
    second=missing(2)
    healthy=add_post(message_id=3)
    with database.get_connection() as conn:
        original=dict(conn.execute('SELECT * FROM messages WHERE id=?',(second,)).fetchone())
    assert database.requeue_missing_jobs_extractions(limit=1)==1
    with database.get_connection() as conn:
        updated=dict(conn.execute('SELECT * FROM messages WHERE id=?',(second,)).fetchone())
        assert updated['ai_status']=='pending'
        assert updated['ai_processed_at'] is None
        for key in ('date','raw_text','cleaned_text','message_id','channel_username','advertio_status'):
            assert updated[key]==original[key]
        assert conn.execute('SELECT COUNT(*) FROM joblist WHERE processed_message_id=?',(healthy,)).fetchone()[0]==1
    assert database.requeue_missing_jobs_extractions()==1
    assert database.requeue_missing_jobs_extractions()==0
    database.initialize_db()
    assert database.requeue_missing_jobs_extractions()==0


def test_existing_delivery_failed_and_other_category_untouched(env):
    service,client=env
    queued=add_post(message_id=1)
    service.enqueue()
    with database.get_connection() as conn:
        conn.execute('DELETE FROM joblist WHERE processed_message_id=?',(queued,))
    failed=missing(2)
    other=missing(3)
    with database.get_connection() as conn:
        conn.execute("UPDATE messages SET ai_status='failed' WHERE id=?",(failed,))
        conn.execute("UPDATE messages SET classification_category='housinglist' WHERE id=?",(other,))
    assert database.requeue_missing_jobs_extractions()==0
    assert not client.sent


@pytest.mark.parametrize('limit',[0,-1,1001])
def test_invalid_batch_rejected(env,limit):
    with pytest.raises(ValueError):
        database.requeue_missing_jobs_extractions(limit)


@pytest.mark.asyncio
async def test_operator_requires_enabled_idle_pipeline(env,monkeypatch):
    from storage.message_repository import MessageRepository
    operations=BackofficeOperations(console_ui=FakeConsole())
    operations.ai.repository=MessageRepository()
    monkeypatch.setattr(operations,'_activity',lambda *a,**kw:None)
    monkeypatch.setattr(config,'is_ai_extraction_enabled',lambda category:False)
    with pytest.raises(ValueError):
        await operations.requeue_missing_jobs_extractions()
    monkeypatch.setattr(config,'is_ai_extraction_enabled',lambda category:True)
    monkeypatch.setattr(operations,'state',lambda name:{'status':'running'})
    with pytest.raises(RuntimeError):
        await operations.requeue_missing_jobs_extractions()
    monkeypatch.setattr(operations,'state',lambda name:{'status':'idle'})
    missing(1)
    assert await operations.requeue_missing_jobs_extractions(limit=1)==1
