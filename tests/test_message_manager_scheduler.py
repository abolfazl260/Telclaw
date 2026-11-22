from datetime import datetime, timezone, timedelta
import pytest
import config
from message_manager import recurrence
from message_manager.scheduler import Worker
from message_manager.models import SendResult, utcnow
from message_manager.validation import ValidationError
from test_message_manager_repository import scheduled


@pytest.mark.parametrize('kind,interval,weekdays,dates', [
    ('once',1,'0',['2026-11-06']),
    ('daily',1,'0',['2026-11-06','2026-11-07','2026-11-08']),
    ('days',3,'0',['2026-11-06','2026-11-09','2026-11-12']),
    ('weekly',1,'0,4',['2026-11-06','2026-11-09','2026-11-13']),
    ('weeks',2,'0,4',['2026-11-06','2026-11-16','2026-11-20']),
])
def test_recurrence_tehran_and_next_after(kind, interval, weekdays, dates):
    spec = recurrence.normalize(dict(date='2026-11-06', time='10:00', recurrence=kind, interval=interval, weekdays=weekdays))
    for index, day in enumerate(dates):
        value = recurrence.at(spec, index)
        assert value.astimezone(recurrence.TEHRAN).isoformat() == day + 'T10:00:00+03:30'
        assert value.hour == 6 and value.minute == 30
        next_value, ordinal = recurrence.next_after(spec, value - timedelta(seconds=1))
        assert (next_value, ordinal) == (value, index)


def test_recurrence_uses_historical_zone_rules_and_end_conditions():
    spec = recurrence.normalize(dict(date='2021-07-01', time='10:00', recurrence='daily', end_count=3))
    assert recurrence.at(spec, 0).hour == 5  # historical Iran DST, not a hard-coded +03:30
    assert recurrence.at(spec, 3) is None
    assert recurrence.next_after(spec, datetime(2026,1,1,tzinfo=timezone.utc))[0] is None
    spec = recurrence.normalize(dict(date='2026-11-06',time='12:00',recurrence='daily',end_date='2026-11-07'))
    assert recurrence.at(spec, 1) and recurrence.at(spec, 2) is None


@pytest.mark.parametrize('data',[{'date':'bad','time':'12:00'}, {'date':'2026-11-06','time':'12:00+03:30'}, {'date':'2026-11-06','time':'12:00','interval':0}, {'date':'2026-11-06','time':'12:00','weekdays':'7'}, {'date':'2026-11-06','time':'12:00','end_count':-1}])
def test_recurrence_validation(data):
    with pytest.raises(ValidationError):
        recurrence.normalize(data)


@pytest.mark.asyncio
async def test_worker_publishes_without_browser_and_keeps_success(mm, monkeypatch):
    local = utcnow().astimezone(recurrence.TEHRAN)
    mid, sid = scheduled(mm, spec={'date':local.date().isoformat(),'time':local.strftime('%H:%M')})
    monkeypatch.setattr(config,'MESSAGE_MANAGER_SEND_SPACING_SECONDS',1)
    worker = Worker(mm.repo, mm.sender)
    await worker.tick()
    assert len(mm.sender.sent) == 1
    assert mm.sender.checked == [1]
    assert mm.repo.details(mid)[1][-1]['status'] == 'sent'
    await worker.tick()
    assert len(mm.sender.sent) == 1


@pytest.mark.asyncio
async def test_worker_rechecks_eligibility_and_target_identity(mm):
    local = utcnow().astimezone(recurrence.TEHRAN)
    mid, sid = scheduled(mm, targets=(1,), spec={'date':local.date().isoformat(),'time':local.strftime('%H:%M')})
    with mm.repo.connection(True) as c:
        c.execute("UPDATE publishing_targets SET chat_id='@changedchannel' WHERE id=1")
    await Worker(mm.repo, mm.sender).tick()
    assert not mm.sender.sent
    assert mm.repo.details(mid)[1][0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_worker_permission_failure_never_calls_send(mm):
    local = utcnow().astimezone(recurrence.TEHRAN)
    mid, sid = scheduled(mm, targets=(1,), spec={'date':local.date().isoformat(),'time':local.strftime('%H:%M')})
    async def denied(target):
        raise ValidationError('Bot lacks permissions')
    mm.sender.check_target = denied
    await Worker(mm.repo, mm.sender).tick()
    assert not mm.sender.sent
    assert mm.repo.details(mid)[1][0]['status'] == 'failed'

@pytest.mark.asyncio
async def test_idle_worker_skips_sqlite_write_transactions(mm):
    """The second idle tick should be read-only: no recurring heartbeat/recovery writes."""
    sql = []
    original_connect = mm.repo.connect

    def traced_connection():
        conn = original_connect()
        conn.set_trace_callback(sql.append)
        return conn

    mm.repo.connect = traced_connection
    worker = Worker(mm.repo, mm.sender)
    assert await worker.tick() == 0  # startup heartbeat and lease recovery
    sql.clear()
    assert await worker.tick() == 0
    assert any('SELECT' in statement.upper() for statement in sql)
    assert not any('BEGIN IMMEDIATE' in statement.upper() for statement in sql)
    assert not any(statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                   for statement in sql)


@pytest.mark.asyncio
async def test_idle_heartbeat_and_lease_recovery_are_periodic(mm, monkeypatch):
    calls = {'heartbeat': 0, 'recover': 0}
    original_heartbeat = mm.repo.worker_heartbeat
    original_recover = mm.repo.recover

    def heartbeat(*args, **kwargs):
        calls['heartbeat'] += 1
        return original_heartbeat(*args, **kwargs)

    def recover(*args, **kwargs):
        calls['recover'] += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(mm.repo, 'worker_heartbeat', heartbeat)
    monkeypatch.setattr(mm.repo, 'recover', recover)
    worker = Worker(mm.repo, mm.sender)
    await worker.tick()
    await worker.tick()
    assert calls == {'heartbeat': 1, 'recover': 1}

    worker._last_heartbeat_at -= 61
    await worker.tick()
    assert calls == {'heartbeat': 2, 'recover': 1}

    worker._last_recovery_at -= 301
    await worker.tick()
    assert calls == {'heartbeat': 2, 'recover': 2}


@pytest.mark.asyncio
async def test_idle_worker_picks_up_newly_activated_schedule(mm):
    worker = Worker(mm.repo, mm.sender)
    assert await worker.tick() == 0
    local = utcnow().astimezone(recurrence.TEHRAN)
    scheduled(mm, targets=(1,),
              spec={'date': local.date().isoformat(), 'time': local.strftime('%H:%M')})
    assert mm.repo.has_due_work()
    assert await worker.tick() == 1
    assert len(mm.sender.sent) == 1


def test_idle_poll_wakes_early_for_approaching_schedule(mm, monkeypatch):
    from message_manager.models import stamp
    monkeypatch.setattr(config, 'MESSAGE_MANAGER_POLL_SECONDS', 15)
    worker = Worker(mm.repo, mm.sender)
    assert worker._idle_sleep_seconds() == 15
    mid, sid = scheduled(mm, targets=(1,),
                         spec={'date': '2099-01-01', 'time': '12:00'})
    with mm.repo.connection(True) as c:
        c.execute('UPDATE mm_schedules SET next_due=? WHERE id=?',
                  (stamp(utcnow() + timedelta(seconds=4)), sid))
    assert 0.5 <= worker._idle_sleep_seconds() <= 4.0
    with mm.repo.connection(True) as c:
        c.execute('UPDATE mm_schedules SET next_due=? WHERE id=?',
                  (stamp(utcnow() + timedelta(minutes=3)), sid))
    assert worker._idle_sleep_seconds() == 15


def test_due_work_probe_respects_retry_backoff_and_paused_schedules(mm):
    from message_manager.models import stamp
    mid, sid = scheduled(mm, targets=(1,))
    mm.repo.schedule_action(sid, 'pause', 42)
    assert not mm.repo.has_due_work()
    mm.repo.schedule_action(sid, 'resume', 42)
    # In this fixture the scheduled date is in the past, so it is due.
    assert mm.repo.has_due_work(now=datetime(2026, 11, 6, 8, 30, tzinfo=timezone.utc))
    mm.repo.materialize(datetime(2026, 11, 6, 8, 30, tzinfo=timezone.utc))
    with mm.repo.connection(True) as c:
        c.execute('UPDATE mm_deliveries SET status=?,next_retry=?',
                  ('retry', stamp(datetime(2026, 11, 6, 8, 31, tzinfo=timezone.utc))))
    assert not mm.repo.has_due_work(now=datetime(2026, 11, 6, 8, 30, tzinfo=timezone.utc))
    assert mm.repo.has_due_work(now=datetime(2026, 11, 6, 8, 31, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_delivery_only_tick_skips_schedule_materialization(mm, monkeypatch):
    """A queued delivery must not reopen schedule write transactions on each send."""
    local = utcnow().astimezone(recurrence.TEHRAN)
    scheduled(mm, targets=(1,),
              spec={'date': local.date().isoformat(), 'time': local.strftime('%H:%M')})
    assert mm.repo.materialize() == 1
    assert mm.repo.due_work() == (False, True)

    def unexpected_materialize(*args, **kwargs):
        raise AssertionError('Existing deliveries must not re-materialize schedules')

    monkeypatch.setattr(mm.repo, 'materialize', unexpected_materialize)
    assert await Worker(mm.repo, mm.sender).tick() == 1
    assert len(mm.sender.sent) == 1
