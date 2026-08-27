from pathlib import Path

from delivery.advertio_client import AdvertioError
from delivery.advertio_service import AdvertioDeliveryService


class FakeRepository:
    def __init__(self, records):
        self.records = records
        self.marked = []
        self.cleared = []

    def get_advertio_pending(self, limit=100, channel_username=None):
        return self.records

    def mark_advertio_result(self, message_id, channel_username, **kwargs):
        self.marked.append((message_id, channel_username, kwargs))

    def clear_media_path(self, message_id, channel_username):
        self.cleared.append((message_id, channel_username))
        return True


def _record(path):
    return {
        "message_id": 101,
        "channel_username": "test_channel",
        "media_path": str(path),
        "housing_data": {},
    }


def _service(repo, result=None, error=None):
    service = AdvertioDeliveryService(client=object(), repository=repo)
    if error is not None:
        def deliver(*args, **kwargs):
            raise error
    else:
        def deliver(*args, **kwargs):
            return result
    service.deliver = deliver
    return service


def test_sent_cleans_media_and_media_path(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")
    repo = FakeRepository([_record(path)])
    result = _service(repo, {"lead_id": "lead-1", "already_existed": False}).deliver_pending(progress=False)
    assert result == {"found": 1, "sent": 1, "already_existed": 0, "failed": 0}
    assert not path.exists()
    assert repo.cleared == [(101, "test_channel")]
    assert repo.marked[0][2]["status"] == "sent"


def test_already_existed_cleans_media_and_media_path(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")
    repo = FakeRepository([_record(path)])
    result = _service(repo, {"lead_id": "lead-1", "already_existed": True}).deliver_pending(progress=False)
    assert result == {"found": 1, "sent": 0, "already_existed": 1, "failed": 0}
    assert not path.exists()
    assert repo.cleared == [(101, "test_channel")]
    assert repo.marked[0][2]["status"] == "already_existed"


def test_retryable_failure_keeps_media_and_media_path(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")
    repo = FakeRepository([_record(path)])
    result = _service(repo, error=AdvertioError("timeout", retryable=True)).deliver_pending(progress=False)
    assert result["failed"] == 1
    assert path.exists()
    assert repo.cleared == []
    assert repo.marked[0][2]["status"] == "retry"


def test_permanent_failure_keeps_media_and_media_path(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")
    repo = FakeRepository([_record(path)])
    result = _service(repo, error=AdvertioError("bad request", retryable=False)).deliver_pending(progress=False)
    assert result["failed"] == 1
    assert path.exists()
    assert repo.cleared == []
    assert repo.marked[0][2]["status"] == "rejected"


def test_missing_media_does_not_fail_delivery_cleanup(tmp_path):
    path = tmp_path / "missing.jpg"
    repo = FakeRepository([_record(path)])
    result = _service(repo, {"lead_id": "lead-1", "already_existed": False}).deliver_pending(progress=False)
    assert result["sent"] == 1
    assert repo.cleared == [(101, "test_channel")]
    assert repo.marked[0][2]["status"] == "sent"


def test_cleanup_oserror_does_not_change_success_status(tmp_path, monkeypatch):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"photo")
    repo = FakeRepository([_record(path)])
    service = _service(repo, {"lead_id": "lead-1", "already_existed": False})

    def fail_unlink(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    result = service.deliver_pending(progress=False)
    assert result["sent"] == 1
    assert repo.cleared == []
    assert repo.marked[0][2]["status"] == "sent"
