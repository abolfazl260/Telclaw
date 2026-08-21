"""Reusable Telegram media download orchestration.

Collection stores media metadata only. This module performs the existing photo
Download operation later, after AI has accepted a listing for Advertio.
"""

from pathlib import Path

import config
from storage import database


def _persist_media_path(record, path):
    channel_username = str(record.get("channel_username") or "").strip()
    message_id = record.get("message_id")
    if not channel_username or message_id is None:
        return
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE messages SET media_path=? WHERE channel_username=? AND message_id=?",
            (str(path), channel_username, int(message_id)),
        )
        conn.commit()
    finally:
        conn.close()


async def download_photo_for_record(client, record):
    """Download the Telegram photo represented by a collected database record.

    Existing media files are reused. The Telegram message is re-fetched by its
    stable channel/message identifiers because the original Telethon Message
    object is no longer available after collection.
    """
    if not record or record.get("media_type") != "photo":
        return record.get("media_path") if record else None

    existing = record.get("media_path")
    if existing and Path(existing).is_file():
        return str(existing)

    channel_username = str(record.get("channel_username") or "").strip().lstrip("@")
    message_id = record.get("message_id")
    if not channel_username or message_id is None:
        raise ValueError("Telegram media metadata is incomplete")

    channel = channel_username.replace("/", "_") or "unknown"
    media_dir = Path(config.MEDIA_DIR) / channel
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / str(message_id)

    # The target is deterministic, so a previous successful download is reused
    # even when an older database record has no media_path value.
    if target.is_file():
        _persist_media_path(record, target)
        return str(target)

    message = await client.get_messages(channel_username, ids=int(message_id))
    if not message or not getattr(message, "photo", None):
        raise ValueError(f"Telegram photo not available for message {message_id}")

    file_size = getattr(getattr(message, "file", None), "size", None)
    if file_size is not None and file_size > config.ADVERTIO_MEDIA_MAX_SIZE:
        raise ValueError(
            f"photo exceeds Advertio media limit: {file_size} bytes"
        )

    downloaded = await message.download_media(file=str(target))
    if not downloaded:
        raise ValueError("Telegram returned no downloaded file")

    path = Path(downloaded)
    if not path.is_file():
        raise ValueError("Downloaded media path does not exist")

    if path.stat().st_size > config.ADVERTIO_MEDIA_MAX_SIZE:
        try:
            path.unlink()
        except OSError:
            pass
        raise ValueError("downloaded photo exceeds Advertio media limit")

    _persist_media_path(record, path)
    return str(path)
