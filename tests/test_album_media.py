"""Regression tests for Telegram albums and Advertio multi-media delivery."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from collection.media_downloader import download_photos_for_record
from delivery.advertio_service import AdvertioDeliveryService
from storage import database


class FakePhotoMessage:
    def __init__(self, message_id, grouped_id, payload=b"photo"):
        self.id = message_id
        self.grouped_id = grouped_id
        self.photo = object()
        self.file = SimpleNamespace(size=len(payload))
        self._payload = payload

    async def download_media(self, file):
        path = Path(file)
        path.write_bytes(self._payload)
        return str(path)


class FakeTelegramClient:
    def __init__(self, messages):
        self.messages = {message.id: message for message in messages}

    async def get_messages(self, _channel, ids):
        if isinstance(ids, int):
            return self.messages.get(ids)
        return [self.messages.get(int(message_id)) for message_id in ids if int(message_id) in self.messages]


@pytest.mark.asyncio
async def test_album_downloads_in_telegram_order_and_persists_paths(tmp_path, monkeypatch):
    db_path = tmp_path / "album.sqlite"
    media_dir = tmp_path / "media"
    monkeypatch.setattr(config, "DB_NAME", str(db_path))
    monkeypatch.setattr(config, "MEDIA_DIR", str(media_dir))
    database.initialize_db()
    database.insert_message(
        "album_channel",
        102,
        "one two three four five six seven eight nine ten",
        "2026-09-29",
        raw_text="one two three four five six seven eight nine ten",
        has_media=True,
        media_type="photo",
        media_group_id="777",
    )

    messages = [
        FakePhotoMessage(103, 777, b"third"),
        FakePhotoMessage(101, 777, b"first"),
        FakePhotoMessage(102, 777, b"second"),
    ]
    record = {
        "channel_username": "album_channel",
        "message_id": 102,
        "media_type": "photo",
        "media_group_id": "777",
        "media_path": None,
        "media_paths": None,
    }

    paths = await download_photos_for_record(FakeTelegramClient(messages), record)

    assert len(paths) == 3
    assert [Path(path).read_bytes() for path in paths] == [b"first", b"second", b"third"]
    assert record["media_path"] == paths[0]
    assert record["media_paths"] == paths

    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT media_path, media_paths FROM messages WHERE channel_username=? AND message_id=?",
            ("album_channel", 102),
        ).fetchone()
    finally:
        conn.close()

    assert row["media_path"] == paths[0]
    assert json.loads(row["media_paths"]) == paths


@pytest.mark.asyncio
async def test_album_is_capped_at_advertio_ten_media_items(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "album-limit.sqlite"))
    database.initialize_db()
    database.insert_message(
        "album_channel",
        105,
        "one two three four five six seven eight nine ten",
        "2026-09-29",
        raw_text="one two three four five six seven eight nine ten",
        has_media=True,
        media_type="photo",
        media_group_id="888",
    )
    messages = [FakePhotoMessage(message_id, 888, str(message_id).encode()) for message_id in range(100, 112)]
    record = {
        "channel_username": "album_channel",
        "message_id": 105,
        "media_type": "photo",
        "media_group_id": "888",
    }

    paths = await download_photos_for_record(FakeTelegramClient(messages), record)

    assert len(paths) == 10
    assert [Path(path).read_bytes() for path in paths] == [
        str(message_id).encode() for message_id in range(100, 110)
    ]


def test_advertio_uploads_media_paths_in_order_and_caps_at_ten(tmp_path):
    uploaded = []

    class Client:
        def upload_media(self, path, source_name):
            uploaded.append((Path(path).name, source_name))
            return f"key-{Path(path).name}"

        def create_lead(self, payload):
            assert payload["mediaKeys"] == [f"key-{index}.jpg" for index in range(10)]
            return {"lead_id": "lead-1", "already_existed": False, "http_status": 201}

    service = AdvertioDeliveryService(client=Client(), repository=object())
    paths = []
    for index in range(12):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"photo")
        paths.append(str(path))

    record = {
        "message_id": 1,
        "message_link": "https://t.me/source/1",
        "sender_username": "source_user",
        "media_paths": paths,
    }
    housing = {
        "listing_type": "rent",
        "property_type": "apartment",
        "bedrooms": 1,
        "price": 2000,
        "currency": "CAD",
        "country_code": "CA",
        "province": "Ontario",
        "city": "Toronto",
        "title": "Apartment",
    }

    service.deliver(record, housing)

    assert [name for name, _ in uploaded] == [f"{index}.jpg" for index in range(10)]


def test_cleanup_removes_every_album_file_and_clears_media_state(tmp_path):
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"photo")
        paths.append(str(path))

    class Repository:
        def __init__(self):
            self.cleared = []

        def clear_media_path(self, message_id, channel_username):
            self.cleared.append((message_id, channel_username))
            return True

    repository = Repository()
    service = AdvertioDeliveryService(client=object(), repository=repository)
    record = {
        "message_id": 55,
        "channel_username": "album_channel",
        "media_path": paths[0],
        "media_paths": paths,
    }

    service._cleanup_delivered_media(record)

    assert not any(Path(path).exists() for path in paths)
    assert repository.cleared == [(55, "album_channel")]
    assert record["media_path"] is None
    assert record["media_paths"] == []



def test_manual_advertio_delivery_lazily_prepares_album_media(tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"

    class Repository:
        def __init__(self):
            self.marked = []
            self.cleared = []

        def get_advertio_pending(self, limit=100, channel_username=None):
            return [{
                "message_id": 77,
                "channel_username": "album_channel",
                "media_type": "photo",
                "media_path": None,
                "media_paths": None,
                "housing_data": {},
            }]

        def mark_advertio_result(self, message_id, channel_username, **kwargs):
            self.marked.append((message_id, channel_username, kwargs))

        def clear_media_path(self, message_id, channel_username):
            self.cleared.append((message_id, channel_username))
            return True

    repository = Repository()
    service = AdvertioDeliveryService(client=object(), repository=repository)
    seen = []

    def downloader(record):
        seen.append(record["message_id"])
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        return [str(first), str(second)]

    def deliver(record, _data):
        assert record["media_paths"] == [str(first), str(second)]
        assert record["media_path"] == str(first)
        return {"lead_id": "lead-77", "already_existed": False}

    service.deliver = deliver
    result = service.deliver_pending(progress=False, media_downloader=downloader)

    assert result == {"found": 1, "sent": 1, "already_existed": 0, "failed": 0}
    assert seen == [77]
    assert repository.marked[0][2]["status"] == "sent"
    assert repository.cleared == [(77, "album_channel")]
    assert not first.exists()
    assert not second.exists()
