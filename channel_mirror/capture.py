"""Opt-in Telethon scan adapter. Capture metadata only; never send/download."""

import asyncio
import logging
import time

from telethon import utils
from telethon.tl import types

from .models import SourceItem, ValidationError, stamp

logger = logging.getLogger("telclaw.channel_mirror")
ALBUM_NEIGHBORS = 10
MAX_ALBUM_WINDOW = 40
RPC_TIMEOUT_SECONDS = 15


class CaptureStopped(Exception):
    pass


def media_item(message, source_peer_id):
    """Read only mirror metadata, with no source text/entities/buttons/sender."""
    if (message is None or isinstance(message, (types.MessageEmpty, types.MessageService))
            or getattr(message, "action", None) is not None or getattr(message, "noforwards", False)):
        return None
    peer = getattr(message, "peer_id", None)
    if peer is None or utils.get_peer_id(peer) != source_peer_id:
        return None
    media = getattr(message, "media", None)
    if not media or getattr(media, "ttl_seconds", None):
        return None
    photo, document = getattr(media, "photo", None), getattr(media, "document", None)
    metadata = {}
    if photo and not isinstance(photo, types.PhotoEmpty):
        kind, metadata["file_id"] = "photo", photo.id
        sizes = getattr(photo, "sizes", None) or []
        if sizes:
            size = sizes[-1]
            for src, dst in (("w", "width"), ("h", "height"), ("size", "size")):
                value = getattr(size, src, None)
                if value is not None:
                    metadata[dst] = value
    elif document and not isinstance(document, types.DocumentEmpty):
        attrs = getattr(document, "attributes", None) or []
        if any(isinstance(a, (types.DocumentAttributeSticker, types.DocumentAttributeAudio,
                              types.DocumentAttributeAnimated)) for a in attrs):
            return None
        videos = [a for a in attrs if isinstance(a, types.DocumentAttributeVideo)]
        if any(a.round_message for a in videos):
            return None
        mime = getattr(document, "mime_type", "") or ""
        kind = "video" if videos or mime.startswith("video/") else "document"
        metadata.update(file_id=document.id, mime_type=mime)
        if getattr(document, "size", None) is not None:
            metadata["size"] = document.size
        for attr in attrs:
            if isinstance(attr, types.DocumentAttributeVideo):
                metadata.update(width=attr.w, height=attr.h, duration=attr.duration)
            elif isinstance(attr, types.DocumentAttributeFilename):
                metadata["file_name"] = attr.file_name
    else:
        return None
    return SourceItem(message.id, message.date, kind, getattr(message, "grouped_id", None), metadata)


class Capture:
    def __init__(self, repository, run_id, source_name, should_stop=None):
        self.repository, self.run_id, self.source_name = repository, run_id, source_name
        self.should_stop = should_stop
        self.source_peer_id, self.entity, self.client = None, None, None
        self.rules, self.error = [], ""
        self._heartbeat = time.monotonic()

    @property
    def active(self):
        return bool(self.rules) and not self.error

    def check_stop(self):
        task = asyncio.current_task()
        if task and task.cancelling():
            raise asyncio.CancelledError("Mirror crawl was stopped")
        if self.should_stop and self.should_stop():
            raise CaptureStopped("Operator stopped the crawl")

    def fail(self, error):
        self.error = str(error)[:2000] or type(error).__name__
        logger.warning("[MIRROR CAPTURE] run=%s failed: %s", self.run_id, self.error)
        self.abort("stopped" if isinstance(error, CaptureStopped) else "mirror_failed", self.error)

    def abort(self, state, error=""):
        try:
            self.repository.abort_crawl(self.run_id, state, error)
        except Exception:
            # A failed DB remains recoverable as an expired, unreleased run.
            logger.warning("[MIRROR CAPTURE] could not finalize run=%s", self.run_id)

    async def _request(self, operation):
        # Reuse the crawler's bounded reconnect policy; no separate polling loop.
        from collection.crawler import MAX_RECONNECT_ATTEMPTS, _retry_telegram_connection

        for attempt in range(MAX_RECONNECT_ATTEMPTS + 1):
            self.check_stop()
            try:
                return await asyncio.wait_for(operation(), timeout=RPC_TIMEOUT_SECONDS)
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                if attempt == MAX_RECONNECT_ATTEMPTS:
                    raise
                await _retry_telegram_connection(self.client, self.source_name, attempt + 1, exc)

    async def bind(self, client, entity):
        self.client, self.entity = client, entity
        if not isinstance(entity, (types.InputPeerChannel, types.PeerChannel, types.Channel)):
            raise ValidationError("Mirror source must resolve to a Telegram channel")
        self.source_peer_id = utils.get_peer_id(entity)
        channel = await self._request(lambda: client.get_entity(entity))
        if utils.get_peer_id(channel) != self.source_peer_id:
            raise ValidationError("Resolved source identity changed")
        self.rules = self.repository.bind_crawl(self.run_id, self.source_peer_id,
                                               copy_allowed=not getattr(channel, "noforwards", False))

    async def observe(self, message):
        if not self.active:
            return
        item = media_item(message, self.source_peer_id)
        if item and stamp(item.date) > min(r["activation_watermark"] for r in self.rules):
            self.repository.stage_crawl_item(self.run_id, item)
            self._heartbeat = time.monotonic()
        elif time.monotonic() - self._heartbeat >= 60:
            self.repository.touch_crawl(self.run_id)
            self._heartbeat = time.monotonic()

    async def prepare_albums(self):
        if not self.active:
            return
        for group in self.repository.crawl_groups(self.run_id):
            self.check_stop()
            start = max(1, group["first_id"] - ALBUM_NEIGHBORS)
            end = group["last_id"] + ALBUM_NEIGHBORS
            ids = list(range(start, end + 1))
            if len(ids) > MAX_ALBUM_WINDOW:
                raise ValidationError("Album exceeds bounded neighbor window")
            messages = await self._request(lambda: self.client.get_messages(self.entity, ids=ids))
            if len(messages) != len(ids):
                raise ValidationError("Incomplete album neighbor response")
            items, excluded, seen = [], False, set()
            for message in messages:
                if message is None or isinstance(message, types.MessageEmpty):
                    continue
                if message.id not in ids or message.id in seen:
                    raise ValidationError("Invalid album neighbor response")
                seen.add(message.id)
                if getattr(message, "grouped_id", None) != group["grouped_id"]:
                    continue
                item = media_item(message, self.source_peer_id)
                if item is None or item.media_kind not in {"photo", "video"}:
                    excluded = True
                else:
                    items.append(item)
            if items and len(items) < 10 and (
                (start > 1 and min(i.message_id for i in items) == start)
                    or max(i.message_id for i in items) == end):
                raise ValidationError("Album reaches bounded neighbor window edge")
            # Missing/deleted singletons and protected/unsupported groups are
            # omitted as a whole. Never turn them into independent posts.
            self.repository.replace_crawl_album(self.run_id, group["grouped_id"],
                () if excluded or len(items) < 2 else tuple(items))
