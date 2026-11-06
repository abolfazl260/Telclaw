from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import json

import pytest
from message_manager import recurrence
from message_manager.models import SendResult, stamp
from message_manager.validation import ValidationError

NOW = datetime(2026, 11, 6, 8, 30, tzinfo=timezone.utc)


def scheduled(mm, targets=(1, 2), spec=None):
    mid = mm.service.save({'title': 'A message', 'text': '<b>Hello</b>'}, 42)
    spec = recurrence.normalize(spec or {'date': '2026-11-06', 'time': '12:00', 'recurrence': 'once'})
    dest = [t for t in mm.repo.targets() if t['id'] in targets]
    sid = mm.repo.schedule(mid, spec, dest, 42)
    return mid, sid


def test_additive_initialize_and_destination_scope(mm):
    with mm.repo.connection(True) as c:
        c.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,text TEXT)')
        c.execute("INSERT INTO messages VALUES(1,'untouched')")
        c.execute("INSERT INTO publishing_rules(name,category,target_id) VALUES('old','transferlist',2)")
    mm.repo.initialize()
    mid, sid = scheduled(mm)
    mm.repo.materialize(NOW)
    with mm.repo.connection() as c:
        assert c.execute('SELECT text FROM messages').fetchone()[0] == 'untouched'
        assert c.execute('SELECT COUNT(*) FROM publishing_rules').fetchone()[0] == 1
        assert c.execute('SELECT enabled FROM publishing_targets WHERE id=1').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM publishing_deliveries').fetchone()[0] == 0


def test_snapshot_survives_edit_target_change_and_reinitialize(mm):
    mid, sid = scheduled(mm)
    mm.service.save({'id': mid, 'version': 1, 'title': 'New', 'text': 'Edited'}, 42)
    with pytest.raises(ValidationError, match='reload'):
        mm.service.save({'id': mid, 'version': 1, 'title': 'Stale', 'text': 'Wrong'}, 42)
    mm.repo.initialize()
    mm.repo.materialize(NOW)
    job = mm.repo.claim(NOW)
    assert job['content']['text'] == '<b>Hello</b>'
    assert job['content']['version'] == 1
    assert job['chat_id'] == '@firstchannel'
    assert mm.repo.get_message(mid)['content']['text'] == 'Edited'


def test_concurrent_materialization_and_claims_are_unique(mm):
    mid, sid = scheduled(mm)
    with ThreadPoolExecutor(4) as pool:
        counts = list(pool.map(lambda _: mm.repo.materialize(NOW), range(4)))
    assert sum(counts) == 1
    with ThreadPoolExecutor(4) as pool:
        jobs = list(pool.map(lambda _: mm.repo.claim(NOW, spacing=0), range(4)))
    claimed = [j for j in jobs if j]
    assert len(claimed) == 2
    assert len({j['id'] for j in claimed}) == 2
    with mm.repo.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM mm_delivery_attempts').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM mm_occurrences').fetchone()[0] == 1


def test_partial_success_retry_and_attempt_cap(mm):
    mid, sid = scheduled(mm)
    mm.repo.materialize(NOW)
    first = mm.repo.claim(NOW, spacing=0)
    mm.repo.finish(first, SendResult('sent', message_id=501), now=NOW)
    second = mm.repo.claim(NOW, spacing=0)
    mm.repo.finish(second, SendResult('retry', 'server refused'), now=NOW)
    assert mm.repo.claim(NOW + timedelta(seconds=29)) is None
    second = mm.repo.claim(NOW + timedelta(seconds=31))
    assert second['attempts'] == 2
    mm.repo.finish(second, SendResult('retry', 'again'), now=NOW + timedelta(seconds=31))
    second = mm.repo.claim(NOW + timedelta(seconds=92))
    mm.repo.finish(second, SendResult('retry', 'last'), now=NOW + timedelta(seconds=92))
    assert mm.repo.claim(NOW + timedelta(minutes=4)) is None
    _, deliveries, attempts = mm.repo.details(mid)
    assert {d['status'] for d in deliveries} == {'sent', 'failed'}
    assert len(attempts) == 4
    assert next(d for d in deliveries if d['target_id'] == 1)['telegram_message_id'] == 501


def test_429_cooldown_is_shared_and_not_shortened(mm):
    scheduled(mm)
    mm.repo.materialize(NOW)
    job = mm.repo.claim(NOW)
    mm.repo.finish(job, SendResult('retry', 'rate limited', retry_after=7200), now=NOW)
    assert mm.repo.claim(NOW + timedelta(seconds=7199)) is None
    with mm.repo.connection() as c:
        assert all(r[0] >= stamp(NOW + timedelta(seconds=7200)) for r in c.execute('SELECT rate_limited_until FROM publishing_targets'))


def test_recover_only_expired_claims_and_never_blindly_retry(mm):
    mid, sid = scheduled(mm, (1,))
    mm.repo.materialize(NOW)
    job = mm.repo.claim(NOW)
    assert mm.repo.recover(NOW + timedelta(seconds=200)) == 0
    assert mm.repo.recover(NOW + timedelta(seconds=301)) == 1
    assert mm.repo.claim(NOW + timedelta(seconds=302)) is None
    with pytest.raises(ValidationError, match='duplicate risk'):
        mm.repo.manual_retry(job['id'], 42)
    assert not mm.repo.finish(job, SendResult('sent', message_id=900), now=NOW + timedelta(seconds=303))
    mm.repo.manual_retry(job['id'], 42, True)
    _, deliveries, attempts = mm.repo.details(mid)
    assert deliveries[0]['status'] == 'retry'
    assert attempts[0]['outcome'] == 'uncertain'
    assert attempts[0]['finished_at']


def test_pause_resume_cancel_and_archive(mm):
    mid, sid = scheduled(mm, (1,))
    mm.repo.schedule_action(sid, 'pause', 42)
    assert mm.repo.materialize(NOW) == 0
    mm.repo.schedule_action(sid, 'resume', 42)
    assert mm.repo.materialize(NOW) == 1
    mm.repo.schedule_action(sid, 'cancel', 42)
    assert mm.repo.claim(NOW) is None
    assert mm.repo.details(mid)[1][0]['status'] == 'cancelled'
    copy = mm.service.duplicate(mid, 42)
    mm.repo.archive(copy, 42)
    with pytest.raises(ValidationError, match='editable draft'):
        mm.repo.schedule(copy, recurrence.normalize({'date':'2026-11-06','time':'12:00'}), mm.repo.targets(), 42)


def test_one_time_missed_window_and_recurring_no_backlog(mm):
    mid, sid = scheduled(mm, (1,))
    mm.repo.materialize(NOW + timedelta(hours=25))
    assert mm.repo.details(mid)[1][0]['status'] == 'missed'
    mid2, sid2 = scheduled(mm, (1,), {'date':'2020-01-01','time':'12:00','recurrence':'daily'})
    mm.repo.materialize(NOW)
    schedules, deliveries, _ = mm.repo.details(mid2)
    assert len(deliveries) == 1 and deliveries[0]['status'] == 'missed'
    assert datetime.fromisoformat(schedules[0]['next_due']) > NOW
    assert mm.repo.materialize(NOW) == 0


def test_pending_claim_respects_missed_policy_after_paused_resume(mm):
    mid, sid = scheduled(mm, (1,), {'date':'2026-11-06','time':'12:00','recurrence':'daily'})
    mm.repo.materialize(NOW)
    mm.repo.schedule_action(sid, 'pause', 42)
    assert mm.repo.claim(NOW) is None
    mm.repo.schedule_action(sid, 'resume', 42)
    assert mm.repo.claim(NOW + timedelta(hours=2)) is None
    assert mm.repo.details(mid)[1][0]['status'] == 'missed'


@pytest.mark.asyncio
async def test_service_confirmation_and_atomic_reschedule(mm):
    mid, sid = scheduled(mm, (1,))
    future = {'date':'2099-01-01','time':'10:00','recurrence':'weekly'}
    with pytest.raises(ValidationError, match='confirmation'):
        await mm.service.activate(mid, future, [1], 42)
    new = await mm.service.activate(mid, future, [1], 42, True, replace_id=sid)
    states = {s['id']:s['state'] for s in mm.repo.details(mid)[0]}
    assert states[sid] == 'cancelled' and states[new] == 'active'


def _claim_process(args):
    import sqlite3
    from message_manager.repository import Repository
    path, now = args
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    repo = Repository(connect)
    repo.materialize(datetime.fromisoformat(now))
    job = repo.claim(datetime.fromisoformat(now),spacing=0)
    return job['id'] if job else None


def test_multiple_processes_cannot_claim_same_delivery(mm):
    import multiprocessing
    import config
    scheduled(mm,(1,))
    context = multiprocessing.get_context('spawn')
    with context.Pool(3) as pool:
        results = pool.map(_claim_process,[(config.DB_NAME,NOW.isoformat())]*3)
    assert len([result for result in results if result]) == 1


def test_module_cooldown_survives_shorter_registry_update(mm):
    scheduled(mm)
    mm.repo.materialize(NOW)
    job = mm.repo.claim(NOW)
    mm.repo.finish(job,SendResult('retry','rate limited',retry_after=7200),now=NOW)
    with mm.repo.connection(True) as c:
        c.execute('UPDATE publishing_targets SET rate_limited_until=?',(stamp(NOW+timedelta(seconds=30)),))
    assert mm.repo.claim(NOW+timedelta(seconds=60)) is None
