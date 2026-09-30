"""Reusable Telegram media download orchestration.

Collection stores media metadata only. Photos are downloaded lazily after AI
validation, immediately before Advertio delivery. Telegram albums are preserved
as an ordered collection with at most 10 photos, matching Advertio's mediaKeys
contract.
"""

import json
from pathlib import Path

import config
from storage import database

ADVERTIO_MAX_MEDIA_ITEMS = 10
ALBUM_ID_SCAN_RADIUS = 32


def _decode_media_paths(record):
    """Return persisted media paths in order, tolerating legacy single-path rows."""
    if not record:
        return []
    value = record.get("media_paths")
    paths = []
    if isinstance(value, (list, tuple)):
        paths = [str(path) for path in value if path]
    elif isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = []
        if isinstance(decoded, list):
            paths = [str(path) for path in decoded if path]

    if not paths and record.get("media_path"):
        paths = [str(record["media_path"])]

    deduped = []
    seen = set()
    for path in paths:
        if path not in seen:
            deduped.append(path)
            seen.add(path)
    return deduped[:ADVERTIO_MAX_MEDIA_ITEMS]


def _has_persisted_collection(record):
    value = record.get("media_paths") if record else None
    if isinstance(value, (list, tuple)):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return False


def _persist_media_paths(record, paths):
    """Persist the ordered local media collection and its legacy first path."""
    channel_username = str(record.get("channel_username") or "").strip()
    message_id = record.get("message_id")
    ordered = [str(path) for path in paths if path][:ADVERTIO_MAX_MEDIA_ITEMS]
    first = ordered[0] if ordered else None
    record["media_path"] = first
    record["media_paths"] = ordered
    if not channel_username or message_id is None:
        return

    conn = database.get_connection()
    try:
        conn.execute(
            """UPDATE messages
                  SET media_path=?, media_paths=?
                WHERE channel_username=? AND message_id=?""",
            (
                first,
                json.dumps(ordered, ensure_ascii=False, separators=(",", ":")),
                channel_username,
                int(message_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_media_path(record, path):
    """Backward-compatible helper retained for existing callers/tests."""
    _persist_media_paths(record, [path] if path else [])


def _as_message_list(value):
    if value is None:
        return []
    if getattr(value, "id", None) is not None:
        return [value]
    try:
        return [item for item in value if item is not None]
    except TypeError:
        return []


async def _telegram_photo_messages(client, channel_username, message_id, media_group_id):
    """Fetch one photo or all photos belonging to the Telegram album in order."""
    anchor = await client.get_messages(channel_username, ids=int(message_id))
    group_id = media_group_id
    if group_id is None and anchor is not None:
        group_id = getattr(anchor, "grouped_id", None)

    if group_id is None:
        if not anchor or not getattr(anchor, "photo", None):
            raise ValueError(f"Telegram photo not available for message {message_id}")
        return [anchor]

    start = max(1, int(message_id) - ALBUM_ID_SCAN_RADIUS)
    stop = int(message_id) + ALBUM_ID_SCAN_RADIUS
    candidates = await client.get_messages(
        channel_username,
        ids=list(range(start, stop + 1)),
    )
    expected_group = str(group_id)
    photos = [
        item
        for item in _as_message_list(candidates)
        if getattr(item, "photo", None)
        and str(getattr(item, "grouped_id", "")) == expected_group
    ]

    if anchor and getattr(anchor, "photo", None) and str(getattr(anchor, "grouped_id", "")) == expected_group:
        photos.append(anchor)

    by_id = {}
    for item in photos:
        item_id = getattr(item, "id", None)
        if item_id is not None:
            by_id[int(item_id)] = item

    ordered = [by_id[key] for key in sorted(by_id)]
    if not ordered:
        raise ValueError(
            f"Telegram album {group_id} has no available photos near message {message_id}"
        )
    return ordered[:ADVERTIO_MAX_MEDIA_ITEMS]


def _existing_download_for_item(media_dir, record_message_id, item_id, is_album):
    """Find a deterministic current/legacy local path for a Telegram photo."""
    candidates = []
    if is_album:
        candidates.append(media_dir / f"{record_message_id}_{item_id}.jpg")
        if int(item_id) == int(record_message_id):
            candidates.extend([
                media_dir / f"{record_message_id}.jpg",
                media_dir / str(record_message_id),
            ])
    else:
        candidates.extend([
            media_dir / f"{record_message_id}.jpg",
            media_dir / str(record_message_id),
        ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


async def download_photos_for_record(client, record):
    """Download the ordered photo collection represented by one collected record.

    A normal photo produces a one-item list. For Telegram albums, siblings sharing
    the same grouped_id are re-fetched and downloaded in Telegram message order.
    Existing persisted files are reused. The returned list is capped at 10 files
    because Advertio accepts at most 10 mediaKeys and uses the first as the card
    image.
    """
    if not record or record.get("media_type") != "photo":
        return _decode_media_paths(record)

    persisted = _decode_media_paths(record)
    if _has_persisted_collection(record) and persisted and all(Path(path).is_file() for path in persisted):
        record["media_paths"] = persisted
        record["media_path"] = persisted[0]
        return persisted

    media_group_id = record.get("media_group_id")
    if media_group_id is None and persisted and all(Path(path).is_file() for path in persisted):
        record["media_paths"] = persisted
        record["media_path"] = persisted[0]
        return persisted

    channel_username = str(record.get("channel_username") or "").strip().lstrip("@")
    message_id = record.get("message_id")
    if not channel_username or message_id is None:
        raise ValueError("Telegram media metadata is incomplete")

    photos = await _telegram_photo_messages(
        client,
        channel_username,
        int(message_id),
        media_group_id,
    )
    is_album = len(photos) > 1 or media_group_id is not None

    channel = channel_username.replace("/", "_") or "unknown"
    media_dir = Path(config.MEDIA_DIR) / channel
    media_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for item in photos:
        item_id = int(getattr(item, "id"))
        existing = _existing_download_for_item(
            media_dir,
            int(message_id),
            item_id,
            is_album,
        )
        if existing is not None:
            if existing.stat().st_size > config.ADVERTIO_MEDIA_MAX_SIZE:
                raise ValueError(
                    f"photo exceeds Advertio media limit: {existing.stat().st_size} bytes"
                )
            paths.append(str(existing))
            continue

        file_size = getattr(getattr(item, "file", None), "size", None)
        if file_size is not None and file_size > config.ADVERTIO_MEDIA_MAX_SIZE:
            raise ValueError(
                f"photo exceeds Advertio media limit: {file_size} bytes"
            )

        target = (
            media_dir / f"{message_id}_{item_id}.jpg"
            if is_album
            else media_dir / f"{message_id}.jpg"
        )
        downloaded = await item.download_media(file=str(target))
        if not downloaded:
            raise ValueError(
                f"Telegram returned no downloaded file for album item {item_id}"
            )

        path = Path(downloaded)
        if not path.is_file():
            raise ValueError(
                f"Downloaded media path does not exist for album item {item_id}"
            )
        if path.stat().st_size > config.ADVERTIO_MEDIA_MAX_SIZE:
            try:
                path.unlink()
            except OSError:
                pass
            raise ValueError("downloaded photo exceeds Advertio media limit")
        paths.append(str(path))

    _persist_media_paths(record, paths)
    return paths


async def download_photo_for_record(client, record):
    """Backward-compatible single-photo API returning the first downloaded path."""
    paths = await download_photos_for_record(client, record)
    return paths[0] if paths else None
