import asyncio

import pytest

from services.scheduler_service import SchedulerService


class FakeAIProcessingService:
    def __init__(self):
        self.media_downloader = None

    def set_media_downloader(self, media_downloader):
        self.media_downloader = media_downloader


@pytest.mark.asyncio
async def test_media_downloader_is_bound_to_scheduler_loop(monkeypatch):
    calls = []

    async def fake_download(client, record):
        calls.append((client, record, asyncio.get_running_loop()))
        await asyncio.sleep(0)
        return "/tmp/test-media"

    monkeypatch.setattr("services.scheduler_service.download_photo_for_record", fake_download)

    ai_service = FakeAIProcessingService()
    client = object()
    loop = asyncio.get_running_loop()

    SchedulerService._bind_media_downloader(ai_service, client, loop)

    assert ai_service.media_downloader is not None
    result = await asyncio.to_thread(
        ai_service.media_downloader,
        {"message_id": 123, "channel_username": "test", "media_type": "photo"},
    )

    assert result == "/tmp/test-media"
    assert len(calls) == 1
    assert calls[0][0] is client
    assert calls[0][1]["message_id"] == 123
    assert calls[0][2] is loop


@pytest.mark.asyncio
async def test_media_downloader_propagates_download_errors(monkeypatch):
    async def fake_download(client, record):
        raise ValueError("telegram download failed")

    monkeypatch.setattr("services.scheduler_service.download_photo_for_record", fake_download)

    ai_service = FakeAIProcessingService()
    SchedulerService._bind_media_downloader(ai_service, object(), asyncio.get_running_loop())

    with pytest.raises(ValueError, match="telegram download failed"):
        await asyncio.to_thread(
            ai_service.media_downloader,
            {"message_id": 456, "channel_username": "test", "media_type": "photo"},
        )
