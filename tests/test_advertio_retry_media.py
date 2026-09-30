"""Regression tests for fail-closed Advertio media retry behavior."""

from pathlib import Path

from delivery.advertio_service import AdvertioDeliveryService


class FakeRepository:
    def __init__(self, records):
        self.records = records
        self.marked = []
        self.cleared = []

    def get_advertio_pending(self, limit=100, channel_username=None):
        return self.records[:limit]

    def mark_advertio_result(self, message_id, channel_username, **kwargs):
        self.marked.append((message_id, channel_username, kwargs))
        return True

    def clear_media_path(self, message_id, channel_username):
        self.cleared.append((message_id, channel_username))
        return True


class GuardClient:
    def __init__(self):
        self.uploaded = []
        self.created = 0

    def upload_media(self, path, source_name):
        assert Path(path).is_file()
        self.uploaded.append((str(path), source_name))
        return f"key-{Path(path).name}"

    def create_lead(self, payload):
        self.created += 1
        return {
            "lead_id": "lead-1",
            "already_existed": False,
            "http_status": 201,
        }


def _housing():
    return {
        "listing_type": "rent",
        "property_type": "apartment",
        "bedrooms": 1,
        "price": 2100,
        "currency": "CAD",
        "country_code": "CA",
        "province": "Ontario",
        "city": "Toronto",
        "title": "Apartment for rent",
    }


def _photo_record(**overrides):
    record = {
        "message_id": 501,
        "channel_username": "housing_channel",
        "message_link": "https://t.me/housing_channel/501",
        "sender_username": "housing_user",
        "media_type": "photo",
        "media_path": None,
        "media_paths": None,
        "advertio_status": "retry",
        "housing_data": _housing(),
    }
    record.update(overrides)
    return record


def test_retry_photo_without_local_file_or_downloader_does_not_create_lead():
    record = _photo_record()
    repo = FakeRepository([record])
    client = GuardClient()
    service = AdvertioDeliveryService(client=client, repository=repo)

    result = service.deliver_pending(progress=False, media_downloader=None)

    assert result == {"found": 1, "sent": 0, "already_existed": 0, "failed": 1}
    assert client.created == 0
    assert client.uploaded == []
    assert repo.marked[-1][2]["status"] == "retry"
    assert "no media downloader" in repo.marked[-1][2]["error"].lower()


def test_retry_photo_download_failure_does_not_create_lead():
    record = _photo_record()
    repo = FakeRepository([record])
    client = GuardClient()
    service = AdvertioDeliveryService(client=client, repository=repo)

    def downloader(_record):
        raise RuntimeError("telegram unavailable")

    result = service.deliver_pending(progress=False, media_downloader=downloader)

    assert result["failed"] == 1
    assert client.created == 0
    assert client.uploaded == []
    assert repo.marked[-1][2]["status"] == "retry"
    assert "telegram unavailable" in repo.marked[-1][2]["error"]


def test_retry_photo_empty_download_result_does_not_create_lead():
    record = _photo_record()
    repo = FakeRepository([record])
    client = GuardClient()
    service = AdvertioDeliveryService(client=client, repository=repo)

    result = service.deliver_pending(progress=False, media_downloader=lambda _record: [])

    assert result["failed"] == 1
    assert client.created == 0
    assert repo.marked[-1][2]["status"] == "retry"
    assert "no usable photo" in repo.marked[-1][2]["error"].lower()


def test_retry_photo_with_stale_path_requires_redownload(tmp_path):
    stale = tmp_path / "deleted.jpg"
    record = _photo_record(media_path=str(stale), media_paths=[str(stale)])
    repo = FakeRepository([record])
    client = GuardClient()
    service = AdvertioDeliveryService(client=client, repository=repo)

    result = service.deliver_pending(progress=False, media_downloader=None)

    assert result["failed"] == 1
    assert client.created == 0
    assert repo.marked[-1][2]["status"] == "retry"


def test_retry_photo_redownloads_then_sends_with_media_key(tmp_path):
    downloaded = tmp_path / "restored.jpg"
    record = _photo_record()
    repo = FakeRepository([record])
    client = GuardClient()
    service = AdvertioDeliveryService(client=client, repository=repo)
    calls = []

    def downloader(current):
        calls.append(current["message_id"])
        downloaded.write_bytes(b"photo")
        return [str(downloaded)]

    result = service.deliver_pending(progress=False, media_downloader=downloader)

    assert result == {"found": 1, "sent": 1, "already_existed": 0, "failed": 0}
    assert calls == [501]
    assert client.created == 1
    assert len(client.uploaded) == 1
    assert repo.marked[-1][2]["status"] == "sent"
    assert repo.cleared == [(501, "housing_channel")]
    assert not downloaded.exists()
