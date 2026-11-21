"""Read/download from the crawler's existing authorized Telethon client."""

from contextlib import asynccontextmanager
import asyncio
import os
from pathlib import Path
import tempfile

from telethon import utils

from services.telegram_session_guard import session_guard
from .capture import media_item
from .models import ValidationError

PHOTO_LIMIT = 10 * 1024 * 1024
FILE_LIMIT = 50 * 1024 * 1024
TOTAL_LIMIT = 10 * FILE_LIMIT


class Source:
    def __init__(self, client):
        self.client = client

    async def inspect(self, job):
        async with session_guard(self.client):
            raw, peer_type = utils.resolve_id(job["source_peer_id"])
            entity = await asyncio.wait_for(self.client.get_input_entity(peer_type(raw)), 15)
            channel = await asyncio.wait_for(self.client.get_entity(entity), 15)
            if utils.get_peer_id(channel) != job["source_peer_id"] or getattr(channel, "noforwards", False):
                raise ValidationError("Source is protected or its identity changed")
            ids = [i["message_id"] for i in job["items"]]
            messages = await asyncio.wait_for(self.client.get_messages(entity, ids=ids), 15)
            if len(messages) != len(ids):
                raise ValidationError("Source media is missing")
            result = []
            for original, expected in zip(messages, job["items"]):
                item = media_item(original, job["source_peer_id"])
                if (item is None or item.message_id != expected["message_id"] or item.media_kind != expected["media_kind"]
                        or item.grouped_id != expected["grouped_id"]):
                    raise ValidationError("Source media is protected, missing or changed")
                old_id = expected.get("media_metadata", {}).get("file_id")
                if old_id is not None and item.media_metadata.get("file_id") != old_id:
                    raise ValidationError("Source media changed after capture")
                result.append((original, item))
            return result

    @asynccontextmanager
    async def download(self, inspected):
        # Only generated paths in a private directory; never use source filenames.
        with tempfile.TemporaryDirectory(prefix="telclaw-mirror-") as folder:
            files, total = [], 0
            async with session_guard(self.client):
                # Recheck protection immediately before any byte download.
                if inspected:
                    channel = await asyncio.wait_for(self.client.get_entity(inspected[0][0].peer_id), 15)
                    if getattr(channel, "noforwards", False):
                        raise ValidationError("Source became protected before download")
                for index, (message, item) in enumerate(inspected):
                    limit = PHOTO_LIMIT if item.media_kind == "photo" else FILE_LIMIT
                    meta = item.media_metadata
                    if meta.get("size", 0) > limit:
                        raise ValidationError("Source media exceeds Bot API upload limit")
                    if item.media_kind == "photo":
                        w, h = meta.get("width", 0), meta.get("height", 0)
                        if w and h and (w + h > 10000 or max(w, h) / min(w, h) > 20):
                            raise ValidationError("Photo dimensions are unsupported; no image transformation is performed")
                    suffix = {"photo": ".jpg", "video": ".mp4", "document": ".bin"}[item.media_kind]
                    path = Path(folder) / (str(index) + suffix)
                    handle = os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")
                    iterator = None
                    size = 0
                    try:
                        iterator = self.client.iter_download(message.media, request_size=64 * 1024)
                        with handle:
                            async for chunk in iterator:
                                if not isinstance(chunk, bytes) or len(chunk) > 1024 * 1024:
                                    raise ValidationError("Invalid media download chunk")
                                size += len(chunk)
                                total += len(chunk)
                                if size > limit or total > TOTAL_LIMIT:
                                    raise ValidationError("Media download exceeds bounded upload limits")
                                handle.write(chunk)
                    finally:
                        handle.close()
                        close = getattr(iterator, "aclose", None) or getattr(iterator, "close", None)
                        if close:
                            await close()
                    if size == 0 or (meta.get("size") and size != meta["size"]):
                        raise ValidationError("Downloaded media is empty or incomplete")
                    files.append((f"media{index}", path, meta.get("mime_type") or "application/octet-stream"))
            yield files
