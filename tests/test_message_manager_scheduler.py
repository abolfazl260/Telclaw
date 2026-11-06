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
