"""Existing crawler -> durable staging -> successful-channel release contracts."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from telethon import utils
from telethon.tl import types

import config
from channel_mirror import Repository, SourceItem, ValidationError
from channel_mirror.capture import Capture, media_item
from collection import crawler
from services.crawl_job_service import CrawlJobService
from services.scheduler_service import SchedulerService
from storage import database

NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
PEER = utils.get_peer_id(types.PeerChannel(777))
DEST = -1009999999999
TEXT = "one two three four five six seven eight nine ten eleven twelve"


def message(mid, kind="photo", *, date=NOW, group=None, username=None, text="", forwarded=False,
            protected=False, bot=False, action=None, attrs=None, peer=777):
    photo = SimpleNamespace(id=mid + 5000, sizes=[]) if kind == "photo" else None
    document = None
    if kind in {"video", "document"}:
        attributes = attrs if attrs is not None else (
            [types.DocumentAttributeVideo(duration=5, w=640, h=480)] if kind == "video" else [])
        document = SimpleNamespace(id=mid + 6000, mime_type="application/octet-stream", size=12345, attributes=attributes)
    return SimpleNamespace(id=mid, date=date, peer_id=types.PeerChannel(peer), grouped_id=group,
        noforwards=protected, action=action, media=SimpleNamespace(photo=photo, document=document, ttl_seconds=None)
        if photo or document else None,
        sender=SimpleNamespace(id=mid + 1000, username=username, bot=bot, broadcast=False),
        fwd_from=SimpleNamespace(from_id=types.PeerChannel(888)) if forwarded else None,
        forward=None, raw_text=text, text=text, message=text, buttons=["never copied"], entities=["never copied"])


class Client:
    def __init__(self, messages, neighbors=None, *, protected=False):
        self.messages = sorted(messages, key=lambda m: m.id, reverse=True)
        self.neighbors = {m.id: m for m in (neighbors if neighbors is not None else messages)}
        self.protected = protected
        self.refetches, self.offsets = [], []
        self.before_yield = None
        self.after_scan = None
        self.connected, self.disconnect_once, self.connect_calls = True, None, 0
        self.failure = None

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True
        self.connect_calls += 1

    async def get_input_entity(self, _name):
        return types.InputPeerChannel(777, 123)

    async def get_entity(self, entity):
        if isinstance(entity, types.InputPeerChannel):
            return types.Channel(id=entity.channel_id, title="Source", photo=types.ChatPhotoEmpty(),
                                 date=NOW, broadcast=True, noforwards=self.protected)
        return SimpleNamespace(bot=False)

    async def iter_messages(self, _entity, *, offset_id=None):
        self.offsets.append(offset_id)
        for item in self.messages:
            if offset_id is not None and item.id >= offset_id:
                continue
            if self.disconnect_once == item.id:
                self.disconnect_once = None
                self.connected = False
                raise ConnectionError("disconnected")
            if self.before_yield:
                self.before_yield(item)
            yield item
        if self.failure:
            raise self.failure
        if self.after_scan:
            self.after_scan()

    async def get_messages(self, _entity, *, ids):
        self.refetches.append(ids)
        return [self.neighbors.get(mid) for mid in ids]


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "mirror-crawl.sqlite"))
    repo = Repository()
    repo.initialize()

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(crawler.asyncio, "sleep", no_sleep)
    return repo


def activate(repo, *, peer=PEER, dest=DEST, name="@source", watermark=NOW - timedelta(hours=1)):
    rid = repo.create_rule(peer, name, dest, "@destination", "Fixed footer", 42, now=watermark)
    repo.set_rule_state(rid, "active", 42, now=watermark)
    return rid


async def run(repo, client, *, mode="all", trigger=None, should_stop=None, source="source"):
    job = CrawlJobService(mirror_repository=repo, dispatch_trigger=trigger)
    result = await job.run_channel(client, source, NOW.date(), NOW.date(), crawl_mode=mode, should_stop=should_stop)
    return job, result


def crawls(repo):
    with repo.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM cm_crawl_runs ORDER BY started_at,id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,ad_ids", [("all", [8, 9, 10, 11]), ("photos_only", [9])])
async def test_media_capture_precedes_every_ad_filter_without_changing_ads(mirror, mode, ad_ids):
    activate(mirror)
    activate(mirror, dest=DEST - 1)
    media = [message(1), message(2, "video"), message(3, "document"), message(4, forwarded=True),
             message(5, username="person", text="too short"), message(6, protected=True),
             message(7, action=object()), message(8, "text", username="person", text=TEXT),
             message(9, username="person", text=TEXT), message(10, "video", username="person", text=TEXT),
             message(11, "document", username="person", text=TEXT), message(12, bot=True, text=TEXT)]
    client = Client(media)
    client.before_yield = lambda _m: assert_no_deliveries(mirror)
    events = []

    def dispatch(event):
        assert mirror.get_crawl(event["run_id"])["state"] == "completed"
        assert len(mirror.list_deliveries()) == 18
        events.append(event)

    job, result = await run(mirror, client, mode=mode, trigger=dispatch)
    assert result["status"] == "completed" and result["mirror"]["deliveries_created"] == 18
    with database.get_connection() as conn:
        ads = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY message_id")]
    assert [r["message_id"] for r in ads] == ad_ids
    assert all(r["raw_text"] == TEXT and r["processing_status"] == "pending" and r["ai_status"] == "waiting" for r in ads)
    assert {r["message_id"]: r["media_type"] for r in ads} == {mid: "photo" if mid == 9 else "document" if mid in (10, 11) else None for mid in ad_ids}
    with mirror.connection() as conn:
        items = [dict(r) for r in conn.execute("SELECT * FROM cm_source_items ORDER BY message_id")]
    assert [r["message_id"] for r in items] == [1, 2, 3, 4, 5, 9, 10, 11, 12]
    assert {r["message_id"]: r["media_kind"] for r in items}[2] == "video"
    assert all("raw_text" not in r and "sender_username" not in r for r in items)
    assert len(events) == 1 and mirror.pending_dispatches() == events
    assert all(d["caption"] == "@destination\n\nFixed footer" for d in mirror.list_deliveries())
    assert job.mirror_status()[0]["crawl_status"] == "waiting_not_scheduled"


def assert_no_deliveries(repo):
    assert repo.list_deliveries() == [] and repo.pending_dispatches() == []


@pytest.mark.asyncio
async def test_low_level_crawl_only_stages_and_requires_opt_in(mirror):
    activate(mirror)
    client = Client([message(1)])
    await crawler.crawl_channel(client, "source", NOW.date(), NOW.date())
    assert crawls(mirror) == []
    run_id = mirror.begin_crawl("source", NOW.date(), NOW.date(), "all")
    capture = Capture(mirror, run_id, "source")
    result = await crawler.crawl_channel(client, "source", NOW.date(), NOW.date(), mirror_capture=capture)
    assert result["status"] == "completed"
    assert_no_deliveries(mirror)
    with mirror.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cm_crawl_items WHERE run_id=?", (run_id,)).fetchone()[0] == 1
    assert mirror.complete_crawl(run_id)["deliveries_created"] == 1


@pytest.mark.asyncio
async def test_disabled_default_does_not_resolve_source_or_capture(mirror):
    mirror.create_rule(PEER, "@source", DEST, "@destination", "", 42, now=NOW)
    client = Client([message(1)])

    async def forbidden(_entity):
        pytest.fail("No mirror Telegram calls without an active rule")

    client.get_entity = forbidden
    await run(mirror, client)
    assert crawls(mirror) == [] and client.refetches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cause,state", [(ValueError("failed"), "failed"), (asyncio.CancelledError(), "interrupted")])
async def test_failed_or_cancelled_crawl_never_releases_partial_items_and_retry_recovers(mirror, cause, state):
    activate(mirror)
    client = Client([message(1)])
    client.failure = cause
    if isinstance(cause, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await run(mirror, client)
    else:
        _, result = await run(mirror, client)
        assert result["status"] == "failed"
    assert_no_deliveries(mirror)
    assert crawls(mirror)[0]["state"] == state
    with mirror.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cm_crawl_items").fetchone()[0] == 1
    client.failure = None
    _, result = await run(Repository(), client)
    assert result["mirror"]["deliveries_created"] == 1
    await run(mirror, client)
    assert len(mirror.list_deliveries()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_end", [False, True])
async def test_operator_stop_including_after_last_message_prevents_release(mirror, empty_end):
    activate(mirror)
    stop = False
    items = [message(2), message(1)]
    client = Client(items[:1] if empty_end else items)

    def end():
        nonlocal stop
        stop = True

    if empty_end:
        client.after_scan = end
    else:
        client.before_yield = lambda m: end() if m.id == 1 else None
    _, result = await run(mirror, client, should_stop=lambda: stop)
    assert result["mirror"]["state"] == "stopped"
    assert crawls(mirror)[0]["state"] == "stopped"
    assert_no_deliveries(mirror)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_ids", [[101], [102, 101, 100]])
async def test_album_refetched_once_in_order_across_scan_boundaries_and_restarts(mirror, seed_ids):
    activate(mirror)
    album = [message(i, "video" if i == 101 else "photo", group=99) for i in (100, 101, 102)]
    client = Client([m for m in album if m.id in seed_ids], album)
    _, result = await run(mirror, client)
    assert result["mirror"]["deliveries_created"] == 1
    assert len(client.refetches) == 1 and len(client.refetches[0]) <= 40
    delivery = mirror.get_delivery(mirror.list_deliveries()[0]["id"])
    assert delivery["identity"] == "album:99"
    assert [i["message_id"] for i in delivery["items"]] == [100, 101, 102]
    assert [i["position"] for i in delivery["items"]] == [0, 1, 2]
    assert all(i["grouped_id"] == 99 for i in delivery["items"])
    await run(Repository(), Client(album))
    assert len(mirror.list_deliveries()) == 1


@pytest.mark.asyncio
async def test_album_date_boundary_is_whole_and_activation_remains_authoritative(mirror):
    activate(mirror, watermark=NOW - timedelta(days=2))
    album = [message(100, date=NOW - timedelta(days=1), group=99), message(101, group=99)]
    await run(mirror, Client(album))
    assert [i["message_id"] for i in mirror.get_delivery(1)["items"]] == [100, 101]
    # A later destination cannot backfill the older member across its watermark.
    activate(mirror, dest=DEST - 1, watermark=NOW - timedelta(hours=1))
    await run(mirror, Client(album))
    assert len(mirror.list_deliveries()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protected,singleton", [(True, False), (False, True)])
async def test_protected_or_singleton_album_is_not_split(mirror, protected, singleton):
    activate(mirror)
    album = [message(100, group=99), message(101, group=99, protected=protected)]
    await run(mirror, Client([album[0]], album[:1] if singleton else album))
    assert_no_deliveries_or_posts(mirror)


def assert_no_deliveries_or_posts(repo):
    assert repo.list_deliveries() == []
    with repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cm_posts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_failed_album_refetch_isolated_and_entire_run_is_unreleased(mirror):
    activate(mirror)
    album = [message(100, group=99), message(101, group=99)]
    client = Client([message(200, username="person", text=TEXT), album[0]], album)

    async def incomplete(_entity, *, ids):
        return [album[0]]

    client.get_messages = incomplete
    _, result = await run(mirror, client)
    assert result["status"] == "completed" and result["saved"] == 1
    assert result["mirror"]["state"] == "mirror_failed"
    assert_no_deliveries(mirror)
    assert_no_deliveries_or_posts(mirror)
    _, result = await run(mirror, Client([message(200), album[0]], album))
    assert result["mirror"]["deliveries_created"] == 2


@pytest.mark.asyncio
async def test_reconnect_keeps_capture_idempotent_and_uses_same_crawl_run(mirror):
    activate(mirror)
    client = Client([message(103), message(102), message(101)])
    client.disconnect_once = 102
    _, result = await run(mirror, client)
    assert result["status"] == "completed" and result["mirror"]["deliveries_created"] == 3
    assert client.offsets == [None, 103] and client.connect_calls == 1
    assert len(crawls(mirror)) == 1
    with mirror.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cm_crawl_items").fetchone()[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stage", "release", "dispatch"])
async def test_mirror_failures_never_fail_or_rerun_ad_collection(mirror, monkeypatch, phase):
    activate(mirror)
    events = []
    if phase == "stage":
        monkeypatch.setattr(mirror, "stage_crawl_item", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("staging failed")))
    elif phase == "release":
        enqueue = mirror._enqueue

        def fail_after_write(*args, **kwargs):
            enqueue(*args, **kwargs)
            raise RuntimeError("release failed after insert")

        monkeypatch.setattr(mirror, "_enqueue", fail_after_write)

    def dispatch(event):
        events.append(event)
        raise RuntimeError("listener failed")

    client = Client([message(1, username="person", text=TEXT)])
    _, result = await run(mirror, client, trigger=dispatch)
    assert result["status"] == "completed" and result["saved"] == 1
    assert client.offsets == [None]
    if phase == "dispatch":
        assert len(mirror.list_deliveries()) == len(events) == len(mirror.pending_dispatches()) == 1
        assert result["mirror"]["dispatch_error"] is True
        assert mirror.acknowledge_dispatch(events[0]["id"])
        assert not mirror.acknowledge_dispatch(events[0]["id"])
    else:
        assert_no_deliveries(mirror)
        assert_no_deliveries_or_posts(mirror)
        assert crawls(mirror)[0]["state"] == "mirror_failed"


@pytest.mark.asyncio
async def test_source_protection_and_stable_id_defeat_username_reassignment(mirror):
    activate(mirror, peer=PEER - 1)
    _, result = await run(mirror, Client([message(1)]))
    assert result["mirror"]["deliveries_created"] == 0
    assert_no_deliveries(mirror)
    activate(mirror)
    await run(mirror, Client([message(1)], protected=True))
    assert_no_deliveries(mirror)
    # A renamed selector still matches the resolved peer, not configuration text.
    _, result = await run(mirror, Client([message(1)]), source="new_source_name")
    assert result["mirror"]["deliveries_created"] == 1


@pytest.mark.asyncio
async def test_watermarks_and_mid_crawl_reactivation_do_not_backfill(mirror):
    rid = activate(mirror)
    client = Client([message(1, date=NOW - timedelta(hours=2)), message(2)])
    await run(mirror, client)
    assert mirror.get_delivery(1)["items"][0]["message_id"] == 2
    client = Client([message(3)])

    def reactivate():
        mirror.set_rule_state(rid, "paused", 42, now=NOW - timedelta(minutes=30))
        mirror.set_rule_state(rid, "active", 42, now=NOW - timedelta(minutes=29))

    client.after_scan = reactivate
    _, result = await run(mirror, client)
    assert result["mirror"]["deliveries_created"] == 0
    assert len(mirror.list_deliveries()) == 1


def test_startup_recovery_expires_only_abandoned_unreleased_runs(mirror):
    activate(mirror)
    old = mirror.begin_crawl("source", NOW.date(), NOW.date(), "all", now=NOW)
    mirror.bind_crawl(old, PEER, now=NOW)
    mirror.stage_crawl_item(old, SourceItem(1, NOW, "photo"), now=NOW)
    fresh = mirror.begin_crawl("source", NOW.date(), NOW.date(), "all", now=NOW + timedelta(minutes=9))
    mirror.initialize()
    assert mirror.recover_crawls(now=NOW + timedelta(minutes=11)) == 1
    assert mirror.get_crawl(old)["state"] == "interrupted"
    assert mirror.get_crawl(fresh)["state"] == "running"
    with pytest.raises(ValidationError, match="no longer active"):
        mirror.complete_crawl(old, now=NOW + timedelta(minutes=11))
    assert_no_deliveries(mirror)


def test_waiting_status_uses_active_schedules_not_enabled_rules_or_old_runs(mirror):
    activate(mirror)
    job = CrawlJobService(mirror_repository=mirror)
    assert job.mirror_status()[0]["crawl_status"] == "waiting_not_scheduled"
    job.register_schedule("other", ["other_source"])
    assert job.mirror_status()[0]["crawl_status"] == "waiting_not_scheduled"
    job.register_schedule("selected", ["@source"])
    assert job.mirror_status()[0]["crawl_status"] == "scheduled"
    rid = mirror.begin_crawl("source", NOW.date(), NOW.date(), "all")
    mirror.bind_crawl(rid, PEER - 1)
    assert job.mirror_status()[0]["crawl_status"] == "waiting_not_scheduled"
    job.unregister_schedule("selected")
    assert job.mirror_status()[0]["scheduled"] is False


@pytest.mark.asyncio
async def test_per_channel_dispatch_precedes_other_channels_and_ai_pipeline(mirror, monkeypatch):
    import services.scheduler_service as scheduler_module

    activate(mirror)
    events, pipelines = [], []

    class Job(CrawlJobService):
        async def run_channel(self, client, channel, *args, **kwargs):
            if channel == "second":
                assert len(events) == 1 and not pipelines
                assert mirror.get_crawl(events[0]["run_id"])["state"] == "completed"
                raise asyncio.CancelledError()
            return await super().run_channel(client, channel, *args, **kwargs)

    scheduler = object.__new__(SchedulerService)
    scheduler.crawl_job = Job(mirror_repository=mirror, dispatch_trigger=events.append)
    scheduler.monitor = SimpleNamespace(report=async_noop)
    scheduler.stage_control = SimpleNamespace(is_skip_requested=lambda _: False, consume_skip=lambda _: None)
    scheduler._run_post_crawl_pipeline = lambda *args: pipelines.append(args)
    monkeypatch.setattr(scheduler_module.random, "shuffle", lambda _: None)
    with pytest.raises(asyncio.CancelledError):
        await scheduler._run_cycle(Client([message(1)]), ["source", "second"], 1, NOW.date(), NOW.date(), "all")
    assert len(events) == 1 and not pipelines


async def async_noop(*args, **kwargs):
    pass


@pytest.mark.asyncio
async def test_scheduler_registration_and_stop_reflect_runtime_state(mirror):
    activate(mirror)
    scheduler = object.__new__(SchedulerService)
    scheduler.crawl_job = CrawlJobService(mirror_repository=mirror)
    scheduler._tasks = {}

    async def waiting(*args, **kwargs):
        await asyncio.Event().wait()

    scheduler._run_cycle = waiting
    task = scheduler.schedule_cycle(Client([]), ["source"], NOW.date(), NOW.date(), interval_minutes=1)
    assert scheduler.crawl_job.mirror_status()[0]["crawl_status"] == "scheduled"
    scheduler.stop_all()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler.crawl_job.mirror_status()[0]["crawl_status"] == "waiting_not_scheduled"


def test_media_typing_excludes_special_documents_and_keeps_safe_metadata():
    video = media_item(message(1, "video", text="original caption"), PEER)
    assert video.media_kind == "video" and video.media_metadata["width"] == 640
    assert "original caption" not in json.dumps(video.media_metadata)
    assert media_item(message(2, "document"), PEER).media_kind == "document"
    for attr in (types.DocumentAttributeAudio(duration=1, voice=True),
                 types.DocumentAttributeAnimated(), types.DocumentAttributeVideo(duration=1, w=10, h=10, round_message=True)):
        assert media_item(message(3, "document", attrs=[attr]), PEER) is None
    assert media_item(message(4, peer=888), PEER) is None
    assert media_item(types.MessageEmpty(5, types.PeerChannel(777)), PEER) is None
    with pytest.raises(ValidationError, match="Unsupported media metadata"):
        SourceItem(1, NOW, "photo", media_metadata={"caption": "do not copy"})


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_or_stop_during_album_refetch_cannot_release(mirror, cancel):
    activate(mirror)
    stop = False
    album = [message(100, group=99), message(101, group=99)]
    client = Client([album[0]], album)
    original = client.get_messages

    async def interrupt(entity, *, ids):
        nonlocal stop
        if cancel:
            raise asyncio.CancelledError()
        stop = True
        return await original(entity, ids=ids)

    client.get_messages = interrupt
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await run(mirror, client)
    else:
        _, result = await run(mirror, client, should_stop=lambda: stop)
        assert result["mirror"]["state"] == "stopped"
    assert_no_deliveries(mirror)
    assert crawls(mirror)[0]["state"] == ("interrupted" if cancel else "stopped")


@pytest.mark.asyncio
async def test_album_network_retry_is_bounded_and_isolated(mirror):
    activate(mirror)
    client = Client([message(100, group=99)])
    attempts = []

    async def disconnected(_entity, *, ids):
        attempts.append(ids)
        raise ConnectionError("neighbor lookup unavailable")

    client.get_messages = disconnected
    _, result = await run(mirror, client)
    assert result["status"] == "completed" and result["mirror"]["state"] == "mirror_failed"
    assert len(attempts) == crawler.MAX_RECONNECT_ATTEMPTS + 1
    assert_no_deliveries(mirror)


def test_concurrent_completed_runs_release_once_per_destination(mirror):
    from concurrent.futures import ThreadPoolExecutor

    activate(mirror)
    run_ids = []
    for _ in range(4):
        rid = mirror.begin_crawl("source", NOW.date(), NOW.date(), "all", now=NOW)
        mirror.bind_crawl(rid, PEER, now=NOW)
        mirror.stage_crawl_item(rid, SourceItem(1, NOW, "photo"), now=NOW)
        run_ids.append(rid)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda rid: mirror.complete_crawl(rid, now=NOW), run_ids))
    assert sum(r["deliveries_created"] for r in results) == 1
    assert len(mirror.list_deliveries()) == 1
    assert len(mirror.pending_dispatches()) == 4
    assert all(mirror.get_crawl(rid)["state"] == "completed" for rid in run_ids)


def test_additive_upgrade_from_foundation_preserves_source_items(tmp_path):
    import sqlite3

    path = str(tmp_path / "foundation.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE cm_source_items (
        id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL,source_peer_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,message_date TEXT NOT NULL,grouped_id INTEGER,
        media_kind TEXT NOT NULL,position INTEGER NOT NULL,crawl_run_id TEXT NOT NULL,
        UNIQUE(source_peer_id,message_id),UNIQUE(post_id,position))""")
    conn.execute("INSERT INTO cm_source_items VALUES(1,1,?,10,?,NULL,'photo',0,'old-run')", (PEER, NOW.isoformat()))
    conn.commit()
    conn.close()
    repo = Repository(lambda: sqlite3.connect(path))
    repo.initialize()
    repo.initialize()
    with repo.connection() as conn:
        row = dict(conn.execute("SELECT * FROM cm_source_items").fetchone())
    assert row["crawl_run_id"] == "old-run" and row["message_id"] == 10 and row["media_metadata"] == "{}"


@pytest.mark.asyncio
async def test_running_status_is_only_live_capture_and_metadata_uses_real_telethon(mirror):
    activate(mirror)
    job = CrawlJobService(mirror_repository=mirror)
    client = Client([message(1)])

    def assert_live(_item):
        assert job.mirror_status()[0]["crawl_status"] == "crawling"

    client.before_yield = assert_live
    await job.run_channel(client, "source", NOW.date(), NOW.date())
    assert job.mirror_status()[0]["crawl_status"] == "waiting_not_scheduled"
    photo = types.Photo(id=999, access_hash=1, file_reference=b"", date=NOW, sizes=[], dc_id=1)
    item = types.Message(id=10, peer_id=types.PeerChannel(777), date=NOW, message="never mirror this caption",
                         media=types.MessageMediaPhoto(photo=photo))
    assert media_item(item, PEER).media_kind == "photo"
    assert media_item(item, PEER).media_metadata == {"file_id": 999}


@pytest.mark.asyncio
@pytest.mark.parametrize("wide", [True, False])
async def test_album_window_bounds_fail_closed(mirror, wide):
    activate(mirror)
    album = [message(100, group=99), message(200 if wide else 90, group=99)]
    client = Client(album if wide else [album[0]], album)
    _, result = await run(mirror, client)
    assert result["status"] == "completed" and result["mirror"]["state"] == "mirror_failed"
    assert len(client.refetches) == (0 if wide else 1)
    assert_no_deliveries(mirror)


@pytest.mark.asyncio
async def test_application_startup_recovers_stale_runs_without_sending(mirror, monkeypatch):
    from unittest.mock import AsyncMock

    import main
    import system_ui
    import backoffice_settings
    import monitoring.telegram_monitor
    import monitoring.transfer_live

    activate(mirror)
    old = mirror.begin_crawl("source", NOW.date(), NOW.date(), "all", now=NOW - timedelta(days=365))
    fresh = mirror.begin_crawl("source", NOW.date(), NOW.date(), "all")
    monkeypatch.setattr(config, "BACKOFFICE_ENABLED", False)
    monkeypatch.setattr(config, "TRANSFER_TELEGRAM_PUBLISH_ENABLED", False)
    monkeypatch.setattr(config, "MESSAGE_MANAGER_ENABLED", False)
    monkeypatch.setattr(backoffice_settings, "apply_persisted_overrides", lambda: None)
    monkeypatch.setattr(system_ui, "SystemConsoleUI", lambda: SimpleNamespace(run=async_noop))
    monitor = SimpleNamespace(enabled=False, start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(monitoring.telegram_monitor, "get_telegram_monitor", lambda: monitor)
    monkeypatch.setattr(monitoring.transfer_live, "install_transfer_live_command", lambda _: None)
    await main._run()
    assert mirror.get_crawl(old)["state"] == "interrupted"
    assert mirror.get_crawl(fresh)["state"] == "running"
    assert_no_deliveries(mirror)
