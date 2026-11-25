import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from storage import crawl_checkpoint as cp
from collection import crawler


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cp.initialize(conn)
    yield conn
    conn.close()


def test_baseline_incremental_daily_reconciliation_and_scope(db):
    today = date.today()
    floor = today - timedelta(days=10)
    now = datetime.now(timezone.utc)
    prior, lower, start, mode = cp.plan(db, "source", 12, "all", floor, today, now)
    assert (prior, lower, start, mode) == (None, 0, floor, "baseline")
    cp.save(db, "source", 12, "all", floor, prior, 100, 10, 10, mode, now)
    prior, lower, _, mode = cp.plan(db, "source", 12, "all", floor, today, now)
    assert (lower, mode) == (100, "incremental")
    cp.save(db, "source", 12, "all", floor, prior, 0, 0, 0, mode, now)
    assert cp.plan(db, "source", 12, "all", floor, today, now)[1] == 100
    assert cp.plan(db, "source", 13, "all", floor, today, now)[3] == "baseline"
    assert cp.plan(db, "source", 12, "photos_only", floor, today, now)[3] == "baseline"
    assert cp.plan(db, "source", 12, "all", floor-timedelta(days=1), today, now)[3] == "baseline"
    assert cp.plan(db, "source", 12, "all", floor, today-timedelta(days=1), now)[3] == "historical"
    prior, lower, start, mode = cp.plan(db, "source", 12, "all", floor, today, now+timedelta(hours=25))
    assert (lower, mode) == (0, "reconcile")
    assert start <= today-timedelta(days=1)


def test_edits_do_not_overwrite_raw_text(db):
    db.execute("CREATE TABLE messages(channel_username TEXT, message_id INTEGER, channel_id INTEGER, raw_text TEXT)")
    db.execute("INSERT INTO messages VALUES('source',100,12,'original')")
    now = datetime.now(timezone.utc)
    assert cp.record_edit(db, "source", 12, 100, now, "changed")
    assert not cp.record_edit(db, "source", 12, 100, now, "changed")
    assert not cp.record_edit(db, "source", 13, 100, now, "changed")
    assert db.execute("SELECT raw_text FROM messages").fetchone()[0] == "original"


@pytest.mark.asyncio
async def test_min_id_survives_reconnect(monkeypatch):
    calls = []
    async def reconnect(*args):
        pass
    monkeypatch.setattr(crawler, "_retry_telegram_connection", reconnect)
    class Client:
        async def iter_messages(self, entity, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                yield SimpleNamespace(id=105)
                raise ConnectionError("temporary")
            yield SimpleNamespace(id=104)
    ids = [m.id async for m in crawler._iter_messages_with_reconnect(Client(), "source", "source", min_id=100)]
    assert ids == [105, 104]
    assert calls == [{"min_id": 100}, {"offset_id": 105, "min_id": 100}]
