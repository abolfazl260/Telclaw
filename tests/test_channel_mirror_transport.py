"""Deterministic Bot API payload, checkpoint, fallback and dispatch contracts."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import aiohttp
import pytest
from telethon import types, utils

import config
from channel_mirror import Repository, SourceItem, SendOutcome
from channel_mirror.bot_api import APIError, BotAPI
from channel_mirror.dispatcher import Dispatcher
from channel_mirror.models import stamp
from channel_mirror.source import Source
from channel_mirror.telegram_sender import Sender
from delivery import bot_limits

PEER = utils.get_peer_id(types.PeerChannel(777))
DEST = -1009999999999
CONTENT = b"original unmodified media bytes"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "transport.sqlite"))
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    repo = Repository()
    repo.initialize()
    with repo.connection(True) as conn:
        bot_limits.initialize(conn)

    async def no_pacing(*args, **kwargs):
        pass

    monkeypatch.setattr(bot_limits, "wait_turn", no_pacing)
    return repo


def delivery(repo, kinds=("photo",), footer="Fixed <b>literal</b> & 😀", mid=100, peer=PEER, claim=True):
    now = datetime.now(timezone.utc)
    existing = next((r for r in repo.list_rules() if r["source_peer_id"] == peer), None)
    if not existing:
        rid = repo.create_rule(peer, "@source", DEST, "@destination", footer, 42, now=now - timedelta(hours=1))
        repo.set_rule_state(rid, "active", 42, now=now - timedelta(hours=1))
    run = repo.begin_crawl("source", now.date(), now.date(), "all")
    repo.bind_crawl(run, peer)
    items = [SourceItem(mid + n, now, kind, mid if len(kinds) > 1 else None) for n, kind in enumerate(kinds)]
    for item in items:
        repo.stage_crawl_item(run, item)
    if len(items) > 1:
        repo.replace_crawl_album(run, mid, items)
    event = repo.complete_crawl(run)["dispatch"]
    return (repo.claim(source_peer_id=peer) if claim else None), event


def due(repo, did):
    with repo.connection(True) as conn:
        conn.execute("UPDATE cm_deliveries SET next_retry=? WHERE id=?", (stamp(datetime.now(timezone.utc) - timedelta(seconds=1)), did))


class API:
    def __init__(self):
        self.calls, self.overrides, self.uploads = [], {}, []

    async def call(self, method, payload, *, files=(), guard=None):
        if guard and not guard():
            raise APIError("Claim is no longer eligible", code=409, retry_after=30)
        self.calls.append((method, payload))
        if files:
            self.uploads.append([(name, path, path.read_bytes(), mime) for name, path, mime in files])
        value = self.overrides.get(method)
        if callable(value):
            value = value(payload)
            if asyncio.iscoroutine(value):
                value = await value
        if isinstance(value, Exception):
            raise value
        if value is not None:
            return value
        if method == "getMe":
            return {"id": 42}
        if method == "getChat":
            return {"id": payload["chat_id"], "type": "channel", "username": "destination"}
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": True}
        if method in {"copyMessages", "sendMediaGroup"}:
            return [{"message_id": 501 + n} for n in range(len(payload.get("message_ids", payload.get("media", []))))]
        return {"message_id": 501}


class FakeSource:
    def __init__(self, client=None):
        self.inspections = 0
        self.markup = None

    async def inspect(self, job):
        self.inspections += 1
        return [(SimpleNamespace(reply_markup=self.markup), SourceItem(i["message_id"], datetime.now(timezone.utc),
                 i["media_kind"], i["grouped_id"])) for i in job["items"]]

    @asynccontextmanager
    async def download(self, inspected):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for n, _ in enumerate(inspected):
                path = Path(folder) / f"{n}.bin"
                path.write_bytes(CONTENT)
                paths.append((f"media{n}", path, "application/octet-stream"))
            yield paths


def source_factory(source):
    return lambda client: source


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["photo", "video", "document"])
@pytest.mark.parametrize("footer", ["", "Fixed <b>literal</b> & 😀"])
async def test_copy_single_always_explicitly_overrides_caption_and_buttons(repo, kind, footer):
    job, _ = delivery(repo, (kind,), footer)
    api = API()
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, object())
    assert result.status == "sent" and result.message_ids == (501,)
    method, payload = next(c for c in api.calls if c[0] == "copyMessage")
    assert payload["caption"] == "@destination" + ("\n\n" + footer if footer else "")
    assert payload["caption_entities"] == [] and "parse_mode" not in payload
    assert payload["reply_markup"] == {"inline_keyboard": []}
    assert not any(m.startswith("forward") for m, _ in api.calls)
    assert repo.finish(job["id"], job["claim_token"], result)
    saved = repo.get_delivery(job["id"])
    assert saved["publish_stage"] == "complete" and saved["media_sent_at"] and saved["caption_set_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kinds", [("photo", "photo"), ("photo", "video")])
async def test_album_copies_group_without_captions_then_checkpoints_then_footer_once(repo, kinds):
    job, _ = delivery(repo, kinds)
    api = API()

    def before_edit(_payload):
        saved = repo.get_delivery(job["id"])
        assert saved["publish_stage"] == "caption_pending"
        assert saved["destination_message_ids"] == [501, 502]
        assert repo.attempts(job["id"])[0]["destination_message_ids"] == [501, 502]
        return {"message_id": 501}

    api.overrides["editMessageCaption"] = before_edit
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, None)
    assert result.status == "sent" and result.message_ids == (501, 502)
    mutations = [(m, p) for m, p in api.calls if m.startswith(("copy", "edit", "send"))]
    assert [m for m, _ in mutations] == ["copyMessages", "editMessageCaption"]
    assert mutations[0][1]["message_ids"] == [100, 101]
    assert mutations[0][1]["remove_caption"] is True
    assert "caption" not in mutations[0][1]
    assert mutations[1][1]["message_id"] == 501 and mutations[1][1]["caption"] == job["caption"]


@pytest.mark.asyncio
async def test_caption_rejection_and_restart_retry_only_edit_never_another_copy(repo):
    job, _ = delivery(repo, ("photo", "video"))
    source, api = FakeSource(), API()
    api.overrides["editMessageCaption"] = APIError("temporary rejection", code=503)
    result = await Sender(repo, api, source_factory(source)).send(job, None)
    assert result.status == "retry"
    repo.finish(job["id"], job["claim_token"], result)
    old = repo.get_delivery(job["id"])
    assert old["status"] == "retry" and old["destination_message_ids"] == [501, 502]
    due(repo, job["id"])
    restarted = Repository()
    job = restarted.claim()
    api.overrides["editMessageCaption"] = {"message_id": 501}
    result = await Sender(restarted, api, source_factory(source)).send(job, None)
    assert result.status == "sent"
    restarted.finish(job["id"], job["claim_token"], result)
    assert source.inspections == 1
    assert [m for m, _ in api.calls].count("copyMessages") == 1
    assert [m for m, _ in api.calls].count("editMessageCaption") == 2
    assert restarted.get_delivery(job["id"])["media_sent_at"] == old["media_sent_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [(APIError("rate limit", code=429, retry_after=90), "retry"),
    (APIError("bad gateway", code=502), "retry"), (APIError("forbidden", code=403), "failed"),
    (APIError("timeout", uncertain=True), "uncertain")])
async def test_copy_error_classification(repo, error, expected):
    job, _ = delivery(repo)
    api = API()
    api.overrides["copyMessage"] = error
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, None)
    assert result.status == expected and result.retry_after == error.retry_after
    repo.finish(job["id"], job["claim_token"], result)
    assert repo.claim() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [[{"message_id": 501}], [{"message_id": 501}, {}], [], None])
async def test_partial_or_unreadable_album_acknowledgement_never_copies_again(repo, result):
    job, event = delivery(repo, ("photo", "video"))
    api = API()
    # None is represented as malformed data because the fake uses None as default.
    api.overrides["copyMessages"] = result if result is not None else "unreadable"
    sender = Sender(repo, api, source_factory(FakeSource()))
    outcome = await sender.send(job, None)
    assert outcome.status == "uncertain"
    repo.finish(job["id"], job["claim_token"], outcome)
    if result:
        assert repo.get_delivery(job["id"])["destination_message_ids"] == [501]
    await Dispatcher(repo, sender).drain(PEER, None)
    assert [m for m, _ in api.calls].count("copyMessages") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", [{"status": "member"}, {"status": "administrator", "can_post_messages": False}])
async def test_permission_denial_is_explicit_and_never_publishes(repo, membership):
    job, _ = delivery(repo)
    api = API()
    api.overrides["getChatMember"] = membership
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, None)
    assert result.status == "failed" and "permission" in result.error
    assert not any(m.startswith(("copy", "send")) for m, _ in api.calls)


@pytest.mark.asyncio
async def test_pause_during_permission_check_is_revalidated_before_any_send(repo):
    job, _ = delivery(repo)
    api = API()

    def pause(_payload):
        repo.set_rule_state(job["rule_id"], "paused", 42)
        return {"status": "administrator", "can_post_messages": True}

    api.overrides["getChatMember"] = pause
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, None)
    assert result.status == "retry"
    assert not any(m.startswith(("copy", "send")) for m, _ in api.calls)


@pytest.mark.asyncio
async def test_destination_public_identity_is_required(repo):
    job, _ = delivery(repo)
    api = API()
    api.overrides["getChat"] = {"id": DEST, "type": "channel", "username": "reassigned"}
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, None)
    assert result.status == "failed" and "identity" in result.error
    assert not any(m.startswith(("copy", "send")) for m, _ in api.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("kinds", [("photo",), ("video",), ("document",), ("photo", "video")])
async def test_crawler_only_fallback_uploads_original_bytes_and_cleans_files(repo, kinds):
    job, _ = delivery(repo, kinds)
    api = API()

    def source_unavailable(payload):
        if payload["chat_id"] == PEER:
            raise APIError("chat not found", code=400)
        return {"id": DEST, "type": "channel", "username": "destination"}

    api.overrides["getChat"] = source_unavailable
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, None)
    assert result.status == "sent"
    for _, path, data, _ in api.uploads[0]:
        assert data == CONTENT and not path.exists()
    assert not any(m.startswith("copy") for m, _ in api.calls)
    method, payload = api.calls[-1]
    if len(kinds) > 1:
        assert method == "sendMediaGroup"
        assert [m["type"] for m in payload["media"]] == list(kinds)
        assert [m["caption"] for m in payload["media"]] == [job["caption"], ""]
    else:
        assert method == {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[kinds[0]]
        assert payload["caption"] == job["caption"] and payload["caption_entities"] == []


@pytest.mark.asyncio
async def test_album_source_keyboard_uses_sanitized_upload_instead_of_copy(repo):
    job, _ = delivery(repo, ("photo", "photo"))
    api, source = API(), FakeSource()
    source.markup = {"inline_keyboard": [[{"url": "https://source", "text": "source"}]]}
    result = await Sender(repo, api, source_factory(source)).send(job, None)
    assert result.status == "sent"
    assert api.calls[-1][0] == "sendMediaGroup" and not any(m == "copyMessages" for m, _ in api.calls)
    assert "source" not in json.dumps(api.calls[-1][1])


@pytest.mark.asyncio
async def test_copy_protection_error_never_uses_fallback(repo):
    job, _ = delivery(repo)
    api, source = API(), FakeSource()
    api.overrides["copyMessage"] = APIError("protected content", code=400)
    result = await Sender(repo, api, source_factory(source)).send(job, None)
    assert result.status == "failed" and not api.uploads


@pytest.mark.asyncio
async def test_cancelled_copy_or_caption_is_uncertain_and_never_retriggered(repo):
    delivery(repo, ("photo", "video"), claim=False)
    api, entered = API(), asyncio.Event()

    async def hold(_payload):
        entered.set()
        await asyncio.Event().wait()

    api.overrides["editMessageCaption"] = hold
    dispatcher = Dispatcher(repo, Sender(repo, api, source_factory(FakeSource())))
    task = asyncio.create_task(dispatcher.drain(PEER, None))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = repo.list_deliveries()[0]
    assert saved["status"] == "uncertain" and json.loads(saved["destination_message_ids"]) == [501, 502]
    await Dispatcher(repo, dispatcher.sender).drain(PEER, None)
    assert [m for m, _ in api.calls].count("copyMessages") == 1


@pytest.mark.asyncio
async def test_trigger_coalescing_no_live_loop_and_sent_not_repeated(repo):
    _, event = delivery(repo, claim=False)
    api = API()
    dispatcher = Dispatcher(repo, Sender(repo, api, source_factory(FakeSource())))
    first = dispatcher.trigger(event, None)
    assert dispatcher.trigger(event, None) is first
    await first
    assert repo.get_delivery(1)["status"] == "sent" and first.done()
    assert not repo.pending_dispatches()
    await dispatcher.trigger(event, None)
    assert [m for m, _ in api.calls].count("copyMessage") == 1


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs):
        if isinstance(self.body, Exception): raise self.body
        return self.body


class Session:
    def __init__(self, response): self.response, self.calls = response, []
    def __call__(self, **kwargs): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception): raise self.response
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize("body,uncertain,code", [
    (ValueError("invalid json"), True, 0), ({}, True, 0), ({"ok": 0}, True, 0),
    ({"ok": True}, True, 0), ({"ok": True, "result": None}, True, 0),
    ({"ok": False, "error_code": 503}, False, 503),
    ({"ok": False, "error_code": 429, "parameters": {"retry_after": 90}}, False, 429)])
async def test_bot_api_distinguishes_structured_rejection_from_ambiguous_ack(repo, body, uncertain, code):
    api = BotAPI("token", session_factory=Session(Response(body)))
    with pytest.raises(APIError) as caught:
        await api.call("copyMessage", {"chat_id": DEST, "caption": "@destination"})
    assert caught.value.uncertain is uncertain and caught.value.code == code
    if code == 429:
        assert caught.value.retry_after == 90 and bot_limits.cooldown_seconds("token") >= 89


@pytest.mark.asyncio
@pytest.mark.parametrize("method,uncertain", [("copyMessage", True), ("sendMediaGroup", True), ("getChat", False)])
async def test_bot_api_connection_drop_classification(repo, method, uncertain):
    api = BotAPI("token", session_factory=Session(aiohttp.ClientConnectionError()))
    with pytest.raises(APIError) as caught:
        await api.call(method, {"chat_id": DEST})
    assert caught.value.uncertain is uncertain


@pytest.mark.asyncio
async def test_post_pacing_claim_guard_prevents_stale_send(repo):
    session = Session(Response({"ok": True, "result": {"message_id": 1}}))
    with pytest.raises(APIError) as caught:
        await BotAPI("token", session_factory=session).call("copyMessage", {"chat_id": DEST}, guard=lambda: False)
    assert caught.value.code == 409 and not session.calls


class SourceClient:
    def __init__(self, kind="document", *, protected=False, data=CONTENT, declared_size=None):
        self.data, self.closed, self.protected = data, False, protected
        attrs = [types.DocumentAttributeVideo(duration=1, w=640, h=480)] if kind == "video" else []
        document = SimpleNamespace(id=9, size=len(data) if declared_size is None else declared_size,
                                   mime_type="video/mp4" if kind == "video" else "application/octet-stream", attributes=attrs)
        photo = SimpleNamespace(id=9, sizes=[SimpleNamespace(w=640, h=480, size=len(data))])
        self.message = SimpleNamespace(id=100, peer_id=types.PeerChannel(777), date=datetime.now(timezone.utc),
            noforwards=False, grouped_id=None, action=None, media=SimpleNamespace(
                photo=photo if kind == "photo" else None, document=document if kind != "photo" else None, ttl_seconds=None))
    async def get_input_entity(self, _peer): return types.InputPeerChannel(777, 1)
    async def get_entity(self, _peer):
        return types.Channel(id=777, title="Source", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), noforwards=self.protected)
    async def get_messages(self, _entity, *, ids): return [self.message]
    async def iter_download(self, _media, **kwargs):
        try:
            for offset in range(0, len(self.data), 4):
                yield self.data[offset:offset + 4]
        finally:
            self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["photo", "video", "document"])
async def test_actual_bounded_download_preserves_bytes_and_private_files(repo, kind, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    job, _ = delivery(repo, (kind,))
    client = SourceClient(kind)
    source = Source(client)
    inspected = await source.inspect(job)
    async with source.download(inspected) as files:
        path = files[0][1]
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(CONTENT).digest()
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not path.exists() and client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["protected", "declared_large", "stream_large", "incomplete"])
async def test_protection_and_large_or_incomplete_fallback_media_are_explicit(repo, case, monkeypatch):
    from channel_mirror import source as module

    job, _ = delivery(repo, ("document",))
    client = SourceClient(protected=case == "protected", declared_size=100 if case in {"declared_large", "incomplete"} else 0)
    if case in {"declared_large", "stream_large"}:
        monkeypatch.setattr(module, "FILE_LIMIT", 8)
    api = API()

    def get_chat(payload):
        if payload["chat_id"] == PEER: raise APIError("chat not found", code=400)
        return {"id": DEST, "type": "channel", "username": "destination"}

    api.overrides["getChat"] = get_chat
    result = await Sender(repo, api).send(job, client)
    assert result.status == "failed" and result.error
    assert not any(m.startswith(("send", "copy")) for m, _ in api.calls)


def test_shared_bot_pacing_is_transactional_token_scoped_and_never_stores_token(repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(bot_limits.time, "time", lambda: 1000.)
    with ThreadPoolExecutor(4) as pool:
        slots = list(pool.map(lambda _: bot_limits.reserve("secret-token"), range(4)))
    assert sorted(round(x, 2) for x in slots) == [0, .05, .10, .15]
    assert bot_limits.reserve("other-token") == 0
    bot_limits.note_429("secret-token", 300)
    bot_limits.note_429("secret-token", 30)
    assert bot_limits.cooldown_seconds("secret-token") == 300
    with pytest.raises(bot_limits.BotCooldown):
        bot_limits.reserve("secret-token")
    with repo.connection() as conn:
        assert "secret-token" not in str([tuple(r) for r in conn.execute("SELECT * FROM telegram_bot_limits")])


@pytest.mark.asyncio
async def test_caption_timeout_requires_review_and_manual_retry_still_edits_only(repo):
    job, _ = delivery(repo, ("photo", "video"))
    api, source = API(), FakeSource()
    api.overrides["editMessageCaption"] = APIError("unreadable edit response", uncertain=True)
    sender = Sender(repo, api, source_factory(source))
    result = await sender.send(job, None)
    assert result.status == "uncertain"
    repo.finish(job["id"], job["claim_token"], result)
    assert repo.claim() is None
    with pytest.raises(ValueError, match="duplicate risk"):
        repo.review(job["id"], "retry", 42, "checked destination")
    repo.review(job["id"], "retry", 42, "album present, caption missing", confirm_duplicate_risk=True)
    job = repo.claim()
    api.overrides["editMessageCaption"] = APIError("Bad Request: message is not modified", code=400)
    result = await Sender(repo, api, source_factory(source)).send(job, None)
    assert result.status == "sent" and result.message_ids == (501, 502)
    assert [m for m, _ in api.calls].count("copyMessages") == 1 and source.inspections == 1


@pytest.mark.asyncio
async def test_copy_cancellation_without_checkpoint_is_uncertain(repo):
    delivery(repo, claim=False)
    api, entered = API(), asyncio.Event()

    async def interrupted(_payload):
        entered.set()
        await asyncio.Event().wait()

    api.overrides["copyMessage"] = interrupted
    dispatcher = Dispatcher(repo, Sender(repo, api, source_factory(FakeSource())))
    task = asyncio.create_task(dispatcher.drain(PEER, None))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = repo.get_delivery(1)
    assert saved["status"] == "uncertain" and saved["destination_message_ids"] == []
    await dispatcher.drain(PEER, None)
    assert [m for m, _ in api.calls].count("copyMessage") == 1


@pytest.mark.asyncio
async def test_checkpoint_expired_lease_preserves_ids_and_never_auto_retries(repo):
    job, _ = delivery(repo, ("photo", "video"))
    assert repo.checkpoint_media(job, [501, 502])
    with repo.connection(True) as conn:
        conn.execute("UPDATE cm_deliveries SET lease_until=? WHERE id=?",
                     (stamp(datetime.now(timezone.utc) - timedelta(seconds=1)), job["id"]))
    assert repo.recover_expired() == 1
    assert repo.get_delivery(job["id"])["destination_message_ids"] == [501, 502]
    assert repo.attempts(job["id"])[0]["destination_message_ids"] == [501, 502]
    assert repo.claim() is None


@pytest.mark.asyncio
async def test_claims_and_drain_are_scoped_to_successfully_crawled_source(repo):
    delivery(repo, claim=False)
    delivery(repo, peer=PEER - 1, claim=False)
    api = API()
    await Dispatcher(repo, Sender(repo, api, source_factory(FakeSource()))).drain(PEER, None)
    assert repo.get_delivery(1)["status"] == "sent" and repo.get_delivery(2)["status"] == "waiting"
    assert repo.pending_dispatches(source_peer_id=PEER - 1)
    assert not repo.pending_dispatches(source_peer_id=PEER)


@pytest.mark.asyncio
async def test_default_runtime_trigger_waits_for_crawl_completion_and_does_not_block_crawler(repo, monkeypatch):
    from channel_mirror import dispatcher as module
    from services.crawl_job_service import CrawlJobService

    delivery(repo, ("document",), claim=False)
    client, api = SourceClient(), API()
    sender = Sender(repo, api, source_factory(FakeSource()))
    monkeypatch.setattr(module, "Sender", lambda repo, bot: sender)

    class Collection:
        async def crawl_channel(self, client, name, start, end, *, mirror_capture, **kwargs):
            await mirror_capture.bind(client, types.InputPeerChannel(777, 1))
            await mirror_capture.observe(client.message)
            assert not api.calls
            return {"status": "completed", "saved": 0}

    job = CrawlJobService(Collection(), mirror_repository=repo)
    result = await job.run_channel(client, "source", datetime.now().date(), datetime.now().date())
    assert result["mirror"]["state"] == "completed"
    await job._dispatcher.tasks[PEER]
    assert repo.get_delivery(1)["status"] == "sent"
    await job.stop_mirror()


@pytest.mark.asyncio
async def test_fallback_file_cleanup_even_when_telegram_rejects_upload(repo):
    job, _ = delivery(repo)
    api = API()

    def chat(payload):
        if payload["chat_id"] == PEER: raise APIError("chat not found", code=400)
        return {"id": DEST, "type": "channel", "username": "destination"}

    api.overrides.update(getChat=chat, sendPhoto=APIError("rejected", code=503))
    result = await Sender(repo, api, source_factory(FakeSource())).send(job, None)
    assert result.status == "retry"
    assert not any(path.exists() for _, path, _, _ in api.uploads[0])


@pytest.mark.asyncio
async def test_session_disconnect_waits_for_guarded_original_media_download(repo):
    from services.account_service import AccountService

    job, _ = delivery(repo, ("document",))
    client, entered, release = SourceClient(), asyncio.Event(), asyncio.Event()
    client.disconnected = False

    async def download(_media, **kwargs):
        entered.set()
        await release.wait()
        yield CONTENT

    async def disconnect():
        client.disconnected = True

    client.iter_download, client.disconnect = download, disconnect
    source = Source(client)
    inspected = await source.inspect(job)

    async def consume():
        async with source.download(inspected): pass

    reader = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(AccountService().disconnect(client))
    await asyncio.sleep(0)
    assert not client.disconnected
    release.set()
    await reader
    await closing
    assert client.disconnected


@pytest.mark.asyncio
async def test_mirror_429_is_shared_with_existing_message_manager_and_routed_publisher(mm):
    from message_manager.telegram_sender import TelegramSender
    from routed_publisher import RoutedPublisher

    with mm.repo.connection(True) as conn:
        bot_limits.initialize(conn)
    response = Response({"ok": False, "error_code": 429, "parameters": {"retry_after": 120}}, 429)
    with pytest.raises(APIError):
        await BotAPI("same-bot", session_factory=Session(response)).call("getChat", {"chat_id": PEER})
    assert TelegramSender(mm.media, token="same-bot").cooldown_seconds() >= 119
    result = await RoutedPublisher(token="same-bot").publish_pending()
    assert result["rate_limited"] is True and result["sent"] == 0
    with mm.repo.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mm_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM publishing_deliveries").fetchone()[0] == 0


def test_busy_shared_registry_defers_publication_instead_of_skipping_pacing(repo):
    with repo.connection(True):
        with pytest.raises(bot_limits.BotCooldown) as caught:
            bot_limits.reserve("test-token")
    assert caught.value.seconds == 1


@pytest.mark.asyncio
async def test_multipart_upload_handles_close_after_unreadable_response(repo, tmp_path):
    path = tmp_path / "original.bin"
    path.write_bytes(CONTENT)
    session = Session(Response(ValueError("bad json")))
    with pytest.raises(APIError) as caught:
        await BotAPI("token", session_factory=session).call("sendDocument",
            {"chat_id": DEST, "caption": "@destination", "caption_entities": []},
            files=[("document", path, "application/octet-stream")])
    assert caught.value.uncertain is True
    handle = session.calls[0][1]["data"]._fields[-1][2]
    assert handle.closed and path.read_bytes() == CONTENT


@pytest.mark.asyncio
async def test_uploaded_albums_reserve_shared_bot_slots_for_every_media_unit(repo, monkeypatch):
    calls = []

    async def pace(token, chat_id, cost):
        calls.append((token, chat_id, cost))

    monkeypatch.setattr(bot_limits, "wait_turn", pace)
    response = Response({"ok": True, "result": [{"message_id": 501}, {"message_id": 502}]})
    await BotAPI("token", session_factory=Session(response)).call("sendMediaGroup",
        {"chat_id": DEST, "media": [{"type": "photo"}, {"type": "video"}]})
    assert calls == [("token", DEST, 2)]
