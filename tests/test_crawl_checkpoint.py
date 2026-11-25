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


@pytest.mark.asyncio
async def test_scheduled_crawl_failure_does_not_advance_and_manual_stays_full(tmp_path, monkeypatch):
    import config
    from storage import database
    from contextlib import closing

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "crawl.sqlite"))
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", False)

    async def no_sleep(*args):
        pass

    monkeypatch.setattr(crawler.asyncio, "sleep", no_sleep)
    today = date.today()

    def message(mid):
        text = f"Message {mid} contains enough words to satisfy the minimum text rule today"
        return SimpleNamespace(
            id=mid, date=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
            sender=SimpleNamespace(id=9000+mid, username=f"user_{mid}", bot=False, broadcast=False),
            raw_text=text, message=text, text=text, fwd_from=None, forward=None,
            media=None, grouped_id=None,
        )

    class Client:
        ids = [100, 99]
        calls = []

        async def get_input_entity(self, channel):
            return SimpleNamespace(channel_id=42, title="Test")

        async def iter_messages(self, entity, **kwargs):
            self.calls.append(kwargs)
            for mid in self.ids:
                if mid > kwargs.get("min_id", 0):
                    yield message(mid)

    client = Client()

    async def crawl(**kwargs):
        return await crawler.crawl_channel(client, "source", today, today, **kwargs)

    first = await crawl(incremental=True)
    assert first["status"] == "completed"
    assert first["checkpoint"] == 100
    assert first["saved"] == 2
    client.ids = [103, 102, 101, 100, 99]
    real_save = crawler.MessageService.save_collected_message

    def fail_one(self, **data):
        if data["message_id"] == 102:
            raise RuntimeError("simulated disk error")
        return real_save(self, **data)

    monkeypatch.setattr(crawler.MessageService, "save_collected_message", fail_one)
    failed = await crawl(incremental=True)
    assert failed["status"] == "failed"
    assert failed["checkpoint"] == 100
    with closing(database.get_connection()) as conn:
        assert conn.execute("SELECT last_message_id FROM crawl_checkpoints").fetchone()[0] == 100

    monkeypatch.setattr(crawler.MessageService, "save_collected_message", real_save)
    recovered = await crawl(incremental=True)
    assert recovered["status"] == "completed"
    assert recovered["checkpoint"] == 103
    assert recovered["saved"] == 1
    assert recovered["new_messages"] == 3
    assert client.calls[-2:] == [{"min_id": 100}, {"min_id": 100}]
    with closing(database.get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5

    manual = await crawl()
    assert manual["scan_mode"] == "full"
    assert client.calls[-1] == {}
