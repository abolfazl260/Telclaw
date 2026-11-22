"""Mirror foundation invariants, including independent SQLite consumers."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Barrier

import pytest

import config
from channel_mirror import Repository, Service, SourceItem, SourcePost, SendOutcome, ValidationError
from channel_mirror.models import stamp

NOW = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
PEER, DEST = -1001234567890, -1009876543210


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "mirror.sqlite"))
    repo = Repository()
    repo.initialize()
    return repo


def rule(repo, dest=DEST, peer=PEER, active=True, now=NOW):
    rid = repo.create_rule(peer, "@sourcechannel", dest, "@destination", "Fixed footer", 42, now=now)
    if active:
        repo.set_rule_state(rid, "active", 42, now=now)
    return rid


def post(mid=10, *, peer=PEER, date=None, run="crawl-1", album=False):
    date = date or NOW + timedelta(seconds=1)
    items = (SourceItem(mid, date, "photo", 777 if album else None),)
    if album:
        items += (SourceItem(mid + 1, date, "video", 777),)
    return SourcePost(peer, items, run, album_complete=album)


def queued(repo, *, album=False):
    rid = rule(repo)
    did, = repo.enqueue(post(album=album), now=NOW)
    return rid, did


def test_default_disabled_and_explicit_activation(mirror):
    rid = rule(mirror, active=False)
    saved = mirror.get_rule(rid)
    assert saved["state"] == "disabled" and saved["activation_watermark"] is None
    assert saved["mode"] == "media_only" and saved["created_by"] == "42"
    assert mirror.enqueue(post(), now=NOW) == []
    assert mirror.claim(now=NOW) is None
    mirror.set_rule_state(rid, "active", 42, now=NOW)
    assert len(mirror.enqueue(post(run="crawl-2"), now=NOW)) == 1
    with pytest.raises(ValidationError, match="already exists"):
        rule(mirror)


def test_mirror_rejects_same_channel_as_source_and_destination(mirror):
    with pytest.raises(ValidationError, match="different channels"):
        mirror.create_rule(PEER, "@sourcechannel", PEER, "@sourcechannel",
                           "", 42, now=NOW)
    assert mirror.list_rules() == []


def test_mirror_rejects_direct_and_multihop_cycles_on_activation(mirror):
    alpha, beta, gamma = PEER, DEST, DEST - 1
    first = mirror.create_rule(alpha, "@channelalpha", beta, "@channelbeta",
                               "", 42, now=NOW)
    reverse = mirror.create_rule(beta, "@channelbeta", alpha, "@channelalpha",
                                 "", 42, now=NOW)
    mirror.set_rule_state(first, "active", 42, now=NOW)
    with pytest.raises(ValidationError, match="cycle"):
        mirror.set_rule_state(reverse, "active", 42, now=NOW)
    assert mirror.get_rule(reverse)["state"] == "disabled"

    second = mirror.create_rule(beta, "@channelbeta", gamma, "@channelgamma",
                                "", 42, now=NOW)
    third = mirror.create_rule(gamma, "@channelgamma", alpha, "@channelalpha",
                               "", 42, now=NOW)
    mirror.set_rule_state(second, "active", 42, now=NOW)
    with pytest.raises(ValidationError, match="cycle"):
        mirror.set_rule_state(third, "active", 42, now=NOW)
    assert mirror.get_rule(third)["state"] == "disabled"


@pytest.mark.parametrize("mode", ["text_only", "text_and_media", "anything"])
def test_extension_modes_are_not_enabled(mirror, mode):
    with pytest.raises(ValidationError, match="media_only"):
        mirror.create_rule(PEER, "@source", DEST, "@destination", "", 42, mode=mode, now=NOW)


@pytest.mark.parametrize("username,footer", [("", ""), ("private", ""), ("@abc", ""),
                                            ("@destination", "😀" * 510)])
def test_public_username_and_caption_limits(mirror, username, footer):
    with pytest.raises(ValidationError):
        mirror.create_rule(PEER, "@source", DEST, username, footer, 42, now=NOW)


def test_rule_validation_and_audit_are_atomic(mirror):
    for kwargs in ({"source_peer_id": "@source"}, {"destination_chat_id": True},
                   {"source_name": ""}, {"actor": None}):
        args = dict(source_peer_id=PEER, source_name="@source", destination_chat_id=DEST,
                    destination_username="@destination", footer="", actor=42)
        args.update(kwargs)
        with pytest.raises(ValidationError):
            mirror.create_rule(**args, now=NOW)
    assert mirror.list_rules() == [] and mirror.audit_log() == []


def test_repeat_scan_restart_and_source_identity(mirror):
    rule(mirror)
    first, = mirror.enqueue(post(), now=NOW)
    mirror.update_rule(1, 42, source_name="@renamedchannel", now=NOW)
    restarted = Repository()
    restarted.initialize()
    assert restarted.enqueue(post(run="another-run"), now=NOW) == []
    assert restarted.list_deliveries()[0]["id"] == first
    delivery = restarted.get_delivery(first)
    assert delivery["source_peer_id"] == PEER
    assert delivery["items"][0]["crawl_run_id"] == "crawl-1"
    assert "text" not in delivery["items"][0] and "sender_username" not in delivery["items"][0]


def test_one_to_many_and_many_to_one_are_destination_scoped(mirror):
    rule(mirror)
    rule(mirror, dest=DEST - 1)
    rule(mirror, peer=PEER - 1)
    assert len(mirror.enqueue(post(), now=NOW)) == 2
    assert len(mirror.enqueue(post(peer=PEER - 1), now=NOW)) == 1
    assert len(mirror.list_deliveries()) == 3
    assert mirror.enqueue(post(run="repeat"), now=NOW) == []


def test_caption_and_chat_snapshots_only_change_for_new_deliveries(mirror):
    rid, first = queued(mirror)
    mirror.update_rule(rid, 99, source_name="@renamed", destination_username="@newdestination",
                       footer="New fixed footer", now=NOW)
    assert mirror.enqueue(post(run="repeat"), now=NOW) == []
    second, = mirror.enqueue(post(20), now=NOW)
    assert mirror.get_delivery(first)["caption"] == "@destination\n\nFixed footer"
    assert mirror.get_delivery(second)["caption"] == "@newdestination\n\nNew fixed footer"
    assert mirror.get_delivery(first)["destination_chat_id"] == DEST
    with pytest.raises(TypeError):
        mirror.update_rule(rid, 42, destination_chat_id=DEST - 1)
    entry = mirror.audit_log()[0]
    assert entry["actor"] == "99" and entry["action"] == "update_rule"
    assert json.loads(entry["details"])["before"]["footer"] == "Fixed footer"


def test_watermark_excludes_initial_history_and_exact_boundary(mirror):
    rule(mirror)
    assert mirror.enqueue(post(1, date=NOW - timedelta(days=1)), now=NOW) == []
    assert mirror.enqueue(post(2, date=NOW), now=NOW) == []
    assert len(mirror.enqueue(post(3), now=NOW)) == 1
    # An edited/rescanned date cannot move a persisted source across the boundary.
    assert mirror.enqueue(post(1), now=NOW) == []
    mirror.initialize()
    assert mirror.enqueue(post(2), now=NOW) == []
    assert mirror.get_rule(1)["activation_watermark"] == stamp(NOW)


def test_pause_resume_and_disable_reactivation_have_no_gap_backfill(mirror):
    rid, first = queued(mirror)
    mirror.set_rule_state(rid, "paused", 42, now=NOW + timedelta(seconds=2))
    assert mirror.claim(now=NOW + timedelta(seconds=3)) is None
    gap = post(20, date=NOW + timedelta(seconds=3))
    assert mirror.enqueue(gap, now=NOW) == []
    mirror.set_rule_state(rid, "active", 42, now=NOW + timedelta(seconds=4))
    assert mirror.enqueue(gap, now=NOW) == []
    assert mirror.claim(now=NOW + timedelta(seconds=5))["id"] == first
    newer, = mirror.enqueue(post(30, date=NOW + timedelta(seconds=5)), now=NOW)
    mirror.set_rule_state(rid, "disabled", 42, now=NOW + timedelta(seconds=6))
    assert mirror.get_delivery(newer)["status"] == "skipped"
    mirror.set_rule_state(rid, "active", 42, now=NOW + timedelta(seconds=8))
    assert mirror.enqueue(post(30, date=NOW + timedelta(seconds=9)), now=NOW) == []
    assert mirror.enqueue(post(40, date=NOW + timedelta(seconds=7)), now=NOW) == []
    assert len(mirror.enqueue(post(50, date=NOW + timedelta(seconds=9)), now=NOW)) == 1
    # A repeated activate call must not advance the boundary.
    mirror.set_rule_state(rid, "active", 42, now=NOW + timedelta(seconds=20))
    assert mirror.get_rule(rid)["activation_watermark"] == stamp(NOW + timedelta(seconds=8))


def test_album_order_complete_identity_and_destination_vector(mirror):
    rid = rule(mirror)
    service = Service(mirror)
    items = (SourceItem(21, NOW + timedelta(seconds=1), "video", 777),
             SourceItem(20, NOW + timedelta(seconds=1), "photo", 777))
    did, = service.observe_media(PEER, items, "run", album_complete=True, now=NOW)
    assert service.observe_media(PEER, tuple(reversed(items)), "repeat", album_complete=True, now=NOW) == []
    job = mirror.claim(now=NOW)
    assert job["id"] == did and job["identity"] == "album:777"
    assert [item["message_id"] for item in job["items"]] == [20, 21]
    assert [item["position"] for item in job["items"]] == [0, 1]
    assert mirror.finish(did, job["claim_token"], SendOutcome("sent", (900, 901)), now=NOW)
    assert mirror.get_delivery(did)["destination_message_ids"] == [900, 901]
    assert mirror.attempts(did)[0]["destination_message_ids"] == [900, 901]
    with pytest.raises(ValidationError, match="membership changed"):
        service.observe_media(PEER, items + (SourceItem(22, items[0].date, "photo", 777),),
                              "later", album_complete=True, now=NOW)


def test_album_straddling_activation_is_not_partially_queued(mirror):
    rule(mirror)
    items = (SourceItem(20, NOW - timedelta(seconds=1), "photo", 777),
             SourceItem(21, NOW + timedelta(seconds=1), "video", 777))
    assert Service(mirror).observe_media(PEER, items, "run", album_complete=True, now=NOW) == []


@pytest.mark.parametrize("items,complete", [
    ((), False), ((None,), False),
    ((SourceItem(1, NOW, "photo", 1),), True),
    ((SourceItem(1, NOW, "photo", 1), SourceItem(2, NOW, "video", 1)), False),
    ((SourceItem(1, NOW, "photo", 1), SourceItem(2, NOW, "video", 2)), True),
    ((SourceItem(1, NOW, "photo", 1), SourceItem(2, NOW, "document", 1)), True),
    ((SourceItem(1, NOW, "photo"), SourceItem(2, NOW, "photo")), False),
    ((SourceItem(1, NOW, "photo", 1), SourceItem(1, NOW, "photo", 1)), True),
])
def test_reject_incomplete_or_unsupported_post(items, complete):
    with pytest.raises(ValidationError):
        SourcePost(PEER, items, "run", complete)


def test_media_only_validation_and_date_normalization(mirror):
    for kind in ("text", "sticker", "audio", ""):
        with pytest.raises(ValidationError):
            SourceItem(1, NOW, kind)
    with pytest.raises(ValidationError, match="aware"):
        SourceItem(1, NOW.replace(tzinfo=None), "photo")
    with pytest.raises(ValidationError, match="Crawl run"):
        SourcePost(PEER, (SourceItem(1, NOW, "photo"),), "")
    assert stamp(NOW.astimezone(timezone(timedelta(hours=3, minutes=30)))) == stamp(NOW)
    rule(mirror)
    assert len(mirror.enqueue(SourcePost(PEER, (SourceItem(1, NOW + timedelta(seconds=1), "document"),), "doc"), now=NOW)) == 1


def test_reclassifying_an_album_member_cannot_create_another_post(mirror):
    queued(mirror, album=True)
    with pytest.raises(ValidationError, match="different post"):
        mirror.enqueue(post(), now=NOW)
    with mirror.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cm_posts").fetchone()[0] == 1


def test_concurrent_scans_and_claims_are_transactional(mirror):
    rule(mirror)
    barrier = Barrier(6)

    def enqueue(_):
        barrier.wait()
        return Repository().enqueue(post(), now=NOW)

    with ThreadPoolExecutor(6) as pool:
        assert sum(len(ids) for ids in pool.map(enqueue, range(6))) == 1
    barrier = Barrier(6)

    def claim(_):
        barrier.wait()
        return Repository().claim(now=NOW)

    with ThreadPoolExecutor(6) as pool:
        jobs = [job for job in pool.map(claim, range(6)) if job]
    assert len(jobs) == 1 and len(mirror.attempts(jobs[0]["id"])) == 1


def _process_consumer(path):
    repo = Repository(lambda: sqlite3.connect(path))
    repo.enqueue(post(), now=NOW)
    return repo.claim(now=NOW)


def test_independent_processes_cannot_duplicate_or_double_claim(mirror):
    from multiprocessing import get_context

    rule(mirror)
    with ProcessPoolExecutor(4, mp_context=get_context("spawn")) as pool:
        jobs = [j for j in pool.map(_process_consumer, [config.DB_NAME] * 4) if j]
    assert len(jobs) == 1 and len(mirror.list_deliveries()) == 1
    assert len(mirror.attempts(jobs[0]["id"])) == 1


def test_definite_failure_retry_backoff_and_attempt_cap(mirror):
    rid, did = queued(mirror)
    first = mirror.claim(now=NOW)
    assert mirror.finish(did, first["claim_token"], SendOutcome("retry", error="not sent"), now=NOW)
    assert mirror.claim(now=NOW + timedelta(seconds=29)) is None
    restarted = Repository()
    second = restarted.claim(now=NOW + timedelta(seconds=30))
    assert second["attempts"] == 2
    restarted.finish(did, second["claim_token"], SendOutcome("retry", retry_after=600), now=NOW + timedelta(seconds=30))
    assert restarted.claim(now=NOW + timedelta(seconds=629)) is None
    third = restarted.claim(now=NOW + timedelta(seconds=630))
    restarted.finish(did, third["claim_token"], SendOutcome("retry"), now=NOW + timedelta(seconds=630))
    assert mirror.get_delivery(did)["status"] == "failed"
    assert len(mirror.attempts(did)) == 3
    assert mirror.claim(now=NOW + timedelta(days=1)) is None


def test_expired_lease_quarantines_and_stale_result_cannot_finish(mirror):
    rid, did = queued(mirror)
    job = mirror.claim(now=NOW, lease_seconds=10)
    restarted = Repository()
    assert restarted.recover_expired(now=NOW + timedelta(seconds=9)) == 0
    assert not restarted.finish(did, job["claim_token"], SendOutcome("sent", (90,)), now=NOW + timedelta(seconds=10))
    assert restarted.recover_expired(now=NOW + timedelta(seconds=10)) == 1
    assert restarted.recover_expired(now=NOW + timedelta(seconds=11)) == 0
    assert restarted.claim(now=NOW + timedelta(days=1)) is None
    assert mirror.get_delivery(did)["status"] == "uncertain"
    assert mirror.attempts(did)[0]["outcome"] == "uncertain"
    with pytest.raises(ValidationError, match="duplicate risk"):
        mirror.review(did, "retry", 42, "Checked destination", now=NOW)
    mirror.review(did, "retry", 42, "Checked destination; absent", confirm_duplicate_risk=True,
                  now=NOW + timedelta(seconds=12))
    fresh = mirror.claim(now=NOW + timedelta(seconds=12))
    assert fresh["claim_token"] != job["claim_token"] and fresh["attempts"] == 2
    assert not mirror.finish(did, job["claim_token"], SendOutcome("sent", (90,)), now=NOW + timedelta(seconds=12))
    assert mirror.finish(did, fresh["claim_token"], SendOutcome("sent", (91,)), now=NOW + timedelta(seconds=12))
    assert not mirror.finish(did, fresh["claim_token"], SendOutcome("sent", (91,)), now=NOW + timedelta(seconds=12))


@pytest.mark.parametrize("result", [SendOutcome("sent", (50,)), SendOutcome("retry", (50,)),
                                   SendOutcome("failed", (50,)), SendOutcome("sent")])
def test_partial_or_missing_album_result_requires_review(mirror, result):
    rid, did = queued(mirror, album=True)
    job = mirror.claim(now=NOW)
    mirror.finish(did, job["claim_token"], result, now=NOW)
    assert mirror.get_delivery(did)["status"] == "uncertain"
    assert mirror.get_delivery(did)["destination_message_ids"] == list(result.message_ids)
    assert mirror.claim(now=NOW + timedelta(hours=1)) is None


@pytest.mark.parametrize("action", ["sent", "skipped", "retry"])
def test_review_retains_attempt_history_and_requires_reason(mirror, action):
    rid, did = queued(mirror, album=True)
    job = mirror.claim(now=NOW)
    mirror.finish(did, job["claim_token"], SendOutcome("uncertain", (50,), error="connection lost"), now=NOW)
    with pytest.raises(ValidationError, match="reason"):
        mirror.review(did, action, 42, "", now=NOW)
    if action == "sent":
        with pytest.raises(ValidationError, match="All destination"):
            mirror.review(did, action, 42, "checked", message_ids=(50,), now=NOW)
    mirror.review(did, action, 42, "Manually inspected Telegram", message_ids=(50, 51) if action == "sent" else (),
                  confirm_duplicate_risk=True, now=NOW)
    delivery = mirror.get_delivery(did)
    assert delivery["status"] == action
    assert delivery["destination_message_ids"] == ([50, 51] if action == "sent" else [50])
    assert mirror.attempts(did)[0]["outcome"] == "uncertain"
    assert mirror.attempts(did)[0]["destination_message_ids"] == [50]
    assert mirror.audit_log()[0]["action"] == "review_" + action


def test_disabled_rule_blocks_review_retry_but_can_confirm_sent(mirror):
    rid, did = queued(mirror)
    job = mirror.claim(now=NOW)
    mirror.set_rule_state(rid, "disabled", 42, now=NOW)
    mirror.finish(did, job["claim_token"], SendOutcome("uncertain"), now=NOW)
    with pytest.raises(ValidationError, match="Activate"):
        mirror.review(did, "retry", 42, "absent", confirm_duplicate_risk=True, now=NOW)
    mirror.review(did, "sent", 42, "found", message_ids=(50,), now=NOW)
    assert mirror.get_delivery(did)["status"] == "sent"


def test_manual_retry_after_cap_grants_one_attempt(mirror):
    rid, did = queued(mirror)
    for seconds in (0, 30, 90):
        when = NOW + timedelta(seconds=seconds)
        job = mirror.claim(now=when)
        mirror.finish(did, job["claim_token"], SendOutcome("retry"), now=when)
    mirror.review(did, "retry", 42, "operator approved", confirm_duplicate_risk=True,
                  now=NOW + timedelta(seconds=100))
    job = mirror.claim(now=NOW + timedelta(seconds=100))
    assert job["attempts"] == 4 and job["attempt_limit"] == 4
    mirror.finish(did, job["claim_token"], SendOutcome("retry"), now=NOW + timedelta(seconds=100))
    assert mirror.get_delivery(did)["status"] == "failed"
    assert len(mirror.attempts(did)) == 4


@pytest.mark.parametrize("status,expected", [("sent", "sent"), ("retry", "skipped"),
                                            ("uncertain", "uncertain"), ("failed", "failed")])
def test_disable_during_in_flight_does_not_erase_outcome(mirror, status, expected):
    rid, did = queued(mirror)
    job = mirror.claim(now=NOW)
    mirror.set_rule_state(rid, "disabled", 42, now=NOW)
    assert mirror.get_delivery(did)["status"] == "sending"
    mirror.finish(did, job["claim_token"], SendOutcome(status, (50,) if status == "sent" else ()), now=NOW)
    assert mirror.get_delivery(did)["status"] == expected


def test_no_schema_or_legacy_scans_in_queue_operations(mirror, monkeypatch):
    from storage import database

    sql = []
    original_connect = mirror.connect

    def connect():
        conn = original_connect()
        conn.set_trace_callback(sql.append)
        return conn

    mirror.connect = connect
    monkeypatch.setattr(database, "initialize_db", lambda: pytest.fail("Legacy initialization during crawl"))
    monkeypatch.setattr(mirror, "initialize", lambda: pytest.fail("Mirror initialization during crawl"))
    rid, did = queued(mirror)
    mirror.enqueue(post(run="repeat"), now=NOW)
    job = mirror.claim(now=NOW)
    mirror.finish(did, job["claim_token"], SendOutcome("sent", (50,)), now=NOW)
    assert not any(s.lstrip().upper().startswith(("CREATE", "ALTER", "DROP")) for s in sql)
    assert not any("FROM messages" in s or "publishing_" in s or "mm_" in s for s in sql)


def test_startup_migration_preserves_all_legacy_rows_and_mirror_states(mm):
    repo = Repository()
    with repo.connection(True) as conn:
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, raw_text TEXT, ai_status TEXT)")
        conn.execute("INSERT INTO messages VALUES(1,NULL,'processing')")
        conn.execute("INSERT INTO publishing_rules(name,category,target_id) VALUES('legacy','transferlist',1)")
        conn.execute("INSERT INTO publishing_deliveries(message_id,target_id,status,updated_at) VALUES(1,1,'uncertain',?)", (stamp(NOW),))
        conn.execute("""INSERT INTO publishing_resends(message_id,target_id,status,created_at,updated_at)
                        VALUES(1,1,'sending',?,?)""", (stamp(NOW), stamp(NOW)))
        conn.execute("""INSERT INTO mm_messages(id,title,content_json,created_by,created_at,updated_at)
                        VALUES(1,'legacy','{}',42,?,?)""", (stamp(NOW), stamp(NOW)))
        conn.execute("""INSERT INTO mm_schedules(id,message_id,spec_json,snapshot_json,state,created_by,created_at)
                        VALUES(1,1,'{}','{}','active',42,?)""", (stamp(NOW),))
        conn.execute("""INSERT INTO mm_occurrences(id,schedule_id,ordinal,due_at,snapshot_json)
                        VALUES(1,1,0,?,'{}')""", (stamp(NOW),))
        conn.execute("""INSERT INTO mm_deliveries(occurrence_id,target_id,chat_id,status,updated_at)
                        VALUES(1,1,'@firstchannel','retry',?)""", (stamp(NOW),))
        # Mirror must not interpret or recover other queues' states.
        conn.execute("INSERT INTO mm_runtime VALUES('legacy','untouched')")
    def snapshot(prefixes):
        with repo.connection() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if any(r[0].startswith(prefix) for prefix in prefixes)]
            return {table: [tuple(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                    for table in tables}
    before = snapshot(("messages", "publishing_", "mm_"))
    repo.initialize()
    rid, did = queued(repo)
    repo.claim(now=NOW, lease_seconds=1)
    for mid, status in enumerate(("sent", "retry", "failed", "uncertain", "skipped"), start=20):
        current, = repo.enqueue(post(mid), now=NOW)
        job = repo.claim(now=NOW)
        assert job["id"] == current
        repo.finish(current, job["claim_token"], SendOutcome(status, (mid,) if status == "sent" else ()), now=NOW)
    repo.enqueue(post(30), now=NOW)
    assert {d["status"] for d in repo.list_deliveries()} == {"waiting", "sending", "sent", "retry", "failed", "uncertain", "skipped"}
    mirror_before = snapshot(("cm_",))
    # Expired sends are recovered explicitly by the future worker, never by DDL.
    repo.initialize()
    repo.initialize()
    assert snapshot(("messages", "publishing_", "mm_")) == before
    assert snapshot(("cm_",)) == mirror_before
    with repo.connection() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_failure_rolls_back_whole_migration(tmp_path, monkeypatch):
    from channel_mirror import repository as module

    path = str(tmp_path / "failure.sqlite")
    repo = Repository(lambda: sqlite3.connect(path))
    monkeypatch.setattr(module, "STATEMENTS", module.STATEMENTS + ("THIS IS INVALID SQL",))
    with pytest.raises(sqlite3.OperationalError):
        repo.initialize()
    with repo.connection() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'cm_%'").fetchall() == []
