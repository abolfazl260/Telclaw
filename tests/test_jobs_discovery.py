from contextlib import closing

import config
from storage import database
from delivery.jobs_discovery import discovery_report
from test_advertio_jobs import env as env, add_post


def test_historical_ai_processed_jobs_require_explicit_backfill(env, monkeypatch):
    service, client = env
    mid=add_post(age_days=1)
    with closing(database.get_connection()) as conn:
        conn.execute("UPDATE advertio_jobs_meta SET value=? WHERE key='deployment_watermark'",(str(mid),))
        conn.commit()
    normal=service.deliver_pending()
    assert normal['found']==0
    assert normal['discovery']['counts']['requires_explicit_backfill']==1
    assert not client.sent
    monkeypatch.setattr(config,'ADVERTIO_JOBS_BACKFILL_ENABLED',True)
    admission=service.enqueue(limit=1,backfill=True)
    assert admission['enqueued']==1
    assert admission['counts']['ready_for_queue']==1
    result=service.deliver_pending()
    assert result['sent']==1
    assert client.sent[0]['autoPublish'] is True
    assert service.enqueue(backfill=True)['enqueued']==0
    assert service.deliver_pending()['found']==0


def test_date_exclusions_use_post_date_not_recent_ai_date(env, monkeypatch):
    service,client=env
    add_post(message_id=101,age_days=30)
    mid=add_post(message_id=102)
    with closing(database.get_connection()) as conn:
        conn.execute("UPDATE messages SET date='bad date' WHERE id=?",(mid,))
        conn.commit()
    monkeypatch.setattr(config,'ADVERTIO_JOBS_BACKFILL_ENABLED',True)
    report=service.enqueue(backfill=True)
    assert report['counts']['outside_source_date_window']==1
    assert report['counts']['invalid_source_date']==1
    assert report['enqueued']==0
    assert not client.sent


def test_effective_backfill_window_is_minimum_and_preview_has_no_side_effects(env,monkeypatch):
    service,client=env
    add_post(age_days=10)
    monkeypatch.setattr(config,'ADVERTIO_JOBS_BACKFILL_DAYS',30)
    with closing(database.get_connection()) as conn:
        report=discovery_report(conn,backfill=True)
        assert report['effective_days']==7
        assert report['counts']['outside_source_date_window']==1
        assert conn.execute('SELECT COUNT(*) FROM advertio_jobs_deliveries').fetchone()[0]==0
    assert not client.sent


def test_diagnostics_distinguish_failed_missing_and_queued(env):
    service,client=env
    add_post(message_id=101)
    failed=add_post(message_id=102)
    missing=add_post(message_id=103)
    with closing(database.get_connection()) as conn:
        conn.execute("UPDATE messages SET ai_status='failed' WHERE id=?",(failed,))
        conn.execute('DELETE FROM joblist WHERE processed_message_id=?',(missing,))
        conn.commit()
    assert service.enqueue()['enqueued']==1
    with closing(database.get_connection()) as conn:
        counts=discovery_report(conn)['counts']
    assert counts['already_in_delivery_queue']==1
    assert counts['awaiting_jobs_extraction']==1
    assert counts['missing_extracted_record']==1
    assert not client.sent


def test_dashboard_explains_original_date_and_backfill_preview(env):
    from backoffice_jobs import jobs_report,jobs_section
    add_post(age_days=30)
    with closing(database.get_connection()) as conn:
        report=jobs_report(conn)
    html=jobs_section(report,lambda *a,**kw:'<form></form>','')
    assert 'Why are Jobs not being sent?' in html
    assert 'original Telegram post date' in html
    assert 'Older than the permitted source-date window' in html
    assert report['backfill_discovery']['counts']['outside_source_date_window']==1
