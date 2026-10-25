import pytest

import config
from storage import database
from storage.message_repository import MessageRepository
from backoffice_operations import BackofficeOperations
from test_backoffice_operations import FakeConsole


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_NAME', str(tmp_path / 'queue.sqlite3'))
    database.initialize_db()
    return MessageRepository()


def add(mid, **changes):
    database.insert_message('jobs', mid, 'Hiring full-time in Toronto', '2026-10-25', processing_status='processed')
    fields = dict(classification_status='processed', classification_category='joblist',
                  ai_status='skipped', ai_error='skipped:category_disabled:joblist', ai_processed_at='old')
    fields.update(changes)
    database.update_message(mid, 'jobs', **fields)


def test_bounded_exact_recovery_preserves_other_outcomes_and_restart(db):
    add(1); add(2)
    for mid, fields in enumerate([
        {'ai_status': 'processed'}, {'ai_status': 'failed'}, {'ai_error': 'skipped:no_text'},
        {'classification_category': 'housinglist'}, {'classification_status': 'failed'},
        {'processing_status': 'pending'},
    ], 3):
        add(mid, **fields)
    assert db.requeue_disabled_jobs_extractions(limit=1) == 1
    assert len(db.get_ai_pending()) == 1
    assert db.requeue_disabled_jobs_extractions(limit=100) == 1
    assert db.requeue_disabled_jobs_extractions() == 0
    database.initialize_db()
    assert len(db.get_ai_pending()) == 2
    with database.get_connection() as conn:
        row = conn.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
        assert row['ai_error'] is None and row['ai_processed_at'] is None
        assert row['text'] == 'Hiring full-time in Toronto'
        assert conn.execute("SELECT COUNT(*) FROM advertio_jobs_deliveries").fetchone()[0] == 0


def test_extracted_and_delivery_records_are_excluded(db):
    add(1); add(2)
    with database.get_connection() as conn:
        ids = [r[0] for r in conn.execute('SELECT id FROM messages ORDER BY id')]
        conn.execute('INSERT INTO joblist(processed_message_id) VALUES(?)', (ids[0],))
        conn.execute("INSERT INTO advertio_jobs_deliveries(processed_message_id,source_name,external_id,updated_at,status) VALUES(?, 'source', '2', 'now', 'sent')", (ids[1],))
    assert db.requeue_disabled_jobs_extractions() == 0


@pytest.mark.parametrize('limit', [0, -1, 1001])
def test_invalid_limit(db, limit):
    with pytest.raises(ValueError):
        db.requeue_disabled_jobs_extractions(limit)


@pytest.mark.asyncio
async def test_controller_requires_enabled_and_idle_then_requeues(db, monkeypatch):
    operations = BackofficeOperations(console_ui=FakeConsole())
    operations.ai.repository = db
    monkeypatch.setattr(operations, '_activity', lambda *a, **kw: None)
    monkeypatch.setattr(config, 'is_ai_extraction_enabled', lambda category: False)
    with pytest.raises(ValueError):
        await operations.requeue_disabled_jobs_extractions()
    monkeypatch.setattr(config, 'is_ai_extraction_enabled', lambda category: True)
    add(1)
    monkeypatch.setattr(operations, 'state', lambda name: {'status': 'running'})
    with pytest.raises(RuntimeError):
        await operations.requeue_disabled_jobs_extractions()
    monkeypatch.setattr(operations, 'state', lambda name: {'status': 'idle'})
    assert await operations.requeue_disabled_jobs_extractions(limit=1) == 1
