"""Advertio data-contract rejections must not look like system failures."""
import logging
from types import SimpleNamespace

from ai.ai_service import AIProcessingService
from delivery.advertio_service import AdvertioDeliveryService


class FakeRepository:
    def __init__(self, records):
        self.records = records
        self.marked = []

    def get_advertio_pending(self, **_kwargs):
        return self.records

    def mark_advertio_result(self, message_id, channel_username, **kwargs):
        self.marked.append((message_id, channel_username, kwargs))
        return True


class NeverCallClient:
    def upload_media(self, *_args, **_kwargs):
        raise AssertionError("Invalid listing must not upload media")

    def create_lead(self, *_args, **_kwargs):
        raise AssertionError("Invalid listing must not reach Advertio API")


def _record():
    return {
        "message_id": 184923,
        "channel_username": "canada_homes",
        "message_link": "https://t.me/canada_homes/184923",
        "media_type": None,
        "housing_data": {
            "listing_type": "rent",
            "property_type": "apartment",
            "bedrooms": None,
            "price": None,
            "currency": None,
            "country_code": "CA",
            "province": None,
            "city": None,
            "title": "Apartment for rent",
        },
    }


def _assert_validation_warning(caplog):
    records = [
        record for record in caplog.records
        if "[ADVERTIO VALIDATION WARNING]" in record.getMessage()
    ]
    assert len(records) == 1
    warning = records[0]
    assert warning.levelno == logging.WARNING
    assert getattr(warning, "notify_admin", False) is True
    assert warning.exc_info is None
    assert "message_id=184923" in warning.getMessage()
    for field in ("bedrooms", "price", "currency=CAD", "province", "city"):
        assert field in warning.getMessage()


def test_inline_housing_mapping_failure_is_warning_and_keeps_rejection(monkeypatch, caplog):
    record = _record()
    repo = FakeRepository([])
    advertio = AdvertioDeliveryService(client=NeverCallClient(), repository=repo)
    ai = AIProcessingService.__new__(AIProcessingService)
    ai.repository = repo
    ai.advertio_service = advertio
    ai.media_downloader = None
    monkeypatch.setattr(
        "ai.ai_service.get_stage_control",
        lambda: SimpleNamespace(is_skip_requested=lambda _stage: False),
    )
    monkeypatch.setattr("delivery.housing_retry.paused_until", lambda: None)

    with caplog.at_level(logging.WARNING):
        result = ai._deliver_to_advertio(record, "housinglist", record["housing_data"])

    assert result == {"attempted": 1, "sent": 0, "already_existed": 0, "failed": 1, "skipped": 0}
    assert repo.marked[0][2]["status"] == "rejected"
    assert "bedrooms" in repo.marked[0][2]["error"]
    _assert_validation_warning(caplog)


def test_batch_housing_mapping_failure_is_warning_and_keeps_rejection(monkeypatch, caplog):
    record = _record()
    repo = FakeRepository([record])
    advertio = AdvertioDeliveryService(client=NeverCallClient(), repository=repo)
    monkeypatch.setattr("delivery.housing_retry.paused_until", lambda: None)

    with caplog.at_level(logging.WARNING):
        result = advertio.deliver_pending(progress=False)

    assert result == {"found": 1, "sent": 0, "already_existed": 0, "failed": 1}
    assert repo.marked[0][2]["status"] == "rejected"
    _assert_validation_warning(caplog)
