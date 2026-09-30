"""Regression tests for automatic AI -> Advertio media cleanup."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import ai.ai_service as ai_module
from ai.ai_service import AIProcessingService
from delivery.advertio_client import AdvertioError
from delivery.advertio_service import AdvertioDeliveryService


class FakeRepository:
    def __init__(self):
        self.marked = []
        self.cleared = []

    def mark_advertio_result(self, message_id, channel_username, **kwargs):
        self.marked.append((message_id, channel_username, kwargs))
        return True

    def clear_media_path(self, message_id, channel_username):
        self.cleared.append((message_id, channel_username))
        return True


class FakeProviderManager:
    provider = "test-provider"


class NoSkipStageControl:
    @staticmethod
    def is_skip_requested(_stage):
        return False


def _record(paths):
    return {
        "message_id": 901,
        "channel_username": "housing_source",
        "message_link": "https://t.me/housing_source/901",
        "sender_username": "housing_user",
        "media_type": "photo",
        "media_path": str(paths[0]),
        "media_paths": [str(path) for path in paths],
    }


def _housing():
    return {
        "listing_type": "rent",
        "property_type": "apartment",
        "bedrooms": 2,
        "price": 2200,
        "currency": "CAD",
        "country_code": "CA",
        "province": "Ontario",
        "city": "Toronto",
        "title": "Two bedroom apartment",
        "description": "Clean apartment in Toronto.",
    }


class SuccessClient:
    def __init__(self, *, already_existed=False):
        self.already_existed = already_existed
        self.uploaded = []

    def upload_media(self, path, source_name):
        self.uploaded.append((path, source_name))
        return f"key-{Path(path).name}"

    def create_lead(self, payload):
        return {
            "lead_id": "lead-901",
            "already_existed": self.already_existed,
            "http_status": 200 if self.already_existed else 201,
        }


@pytest.mark.parametrize(
    ("already_existed", "expected_status"),
    [(False, "sent"), (True, "already_existed")],
)
def test_automatic_ai_advertio_success_cleans_all_local_media(
    tmp_path, monkeypatch, already_existed, expected_status
):
    monkeypatch.setattr(ai_module, "get_stage_control", lambda: NoSkipStageControl())

    paths = []
    for index in range(3):
        path = tmp_path / f"album-{index}.jpg"
        path.write_bytes(f"photo-{index}".encode())
        paths.append(path)

    repository = FakeRepository()
    client = SuccessClient(already_existed=already_existed)
    advertio = AdvertioDeliveryService(client=client, repository=repository)
    service = AIProcessingService(
        repository=repository,
        provider_manager=FakeProviderManager(),
        advertio_service=advertio,
    )
    record = _record(paths)

    result = service._deliver_to_advertio(record, "housinglist", _housing())

    assert result["failed"] == 0
    assert result[expected_status] == 1
    assert not any(path.exists() for path in paths)
    assert repository.cleared == [(901, "housing_source")]
    assert repository.marked[-1][2]["status"] == expected_status
    assert record["media_path"] is None
    assert record["media_paths"] == []


def test_automatic_ai_advertio_failure_keeps_local_media_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "get_stage_control", lambda: NoSkipStageControl())

    paths = []
    for index in range(2):
        path = tmp_path / f"retry-{index}.jpg"
        path.write_bytes(b"photo")
        paths.append(path)

    class RetryClient:
        def upload_media(self, path, source_name):
            return f"key-{Path(path).name}"

        def create_lead(self, payload):
            raise AdvertioError("temporary upstream error", status=503, retryable=True)

    repository = FakeRepository()
    advertio = AdvertioDeliveryService(client=RetryClient(), repository=repository)
    service = AIProcessingService(
        repository=repository,
        provider_manager=FakeProviderManager(),
        advertio_service=advertio,
    )
    record = _record(paths)

    result = service._deliver_to_advertio(record, "housinglist", _housing())

    assert result["failed"] == 1
    assert all(path.exists() for path in paths)
    assert repository.cleared == []
    assert repository.marked[-1][2]["status"] == "retry"
    assert record["media_path"] == str(paths[0])
    assert record["media_paths"] == [str(path) for path in paths]
