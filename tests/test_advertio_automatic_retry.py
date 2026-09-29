"""Regression tests for automatic Advertio waiting/retry delivery."""

from datetime import datetime, timezone

import pytest

import config
from services.scheduler_service import SchedulerService
from storage import database


def _insert_housing_message(message_id, *, advertio_status, ai_processed_at, advertio_processed_at=None):
    database.insert_message(
        "housing_channel",
        message_id,
        "one two three four five six seven eight nine ten",
        "2026-09-29",
        raw_text="one two three four five six seven eight nine ten",
    )
    database.update_message(
        message_id,
        "housing_channel",
        processing_status="processed",
        classification_status="processed",
        classification_category="housinglist",
        ai_status="processed",
        ai_category="housinglist",
        ai_processed_at=ai_processed_at,
        advertio_status=advertio_status,
        advertio_processed_at=advertio_processed_at,
    )
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM messages WHERE channel_username=? AND message_id=?",
            ("housing_channel", message_id),
        ).fetchone()
    finally:
        conn.close()
    database.save_category_record(row["id"], "housinglist", {})


def test_automatic_cutoff_selects_only_work_from_before_current_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "retry-cutoff.sqlite"))
    database.initialize_db()

    old = "2026-09-29T01:00:00+00:00"
    new = "2026-09-29T09:00:00+00:00"
    cutoff = "2026-09-29T05:00:00+00:00"

    _insert_housing_message(
        1,
        advertio_status="retry",
        ai_processed_at=old,
        advertio_processed_at=old,
    )
    _insert_housing_message(
        2,
        advertio_status="retry",
        ai_processed_at=old,
        advertio_processed_at=new,
    )
    _insert_housing_message(
        3,
        advertio_status="waiting",
        ai_processed_at=old,
    )
    _insert_housing_message(
        4,
        advertio_status="waiting",
        ai_processed_at=new,
    )

    rows = database.get_advertio_pending_messages(
        limit=100,
        before_datetime=cutoff,
    )

    assert [row["message_id"] for row in rows] == [1, 3]


class FakeMonitor:
    def __init__(self):
        self.reports = []

    async def report(self, stage, stats):
        self.reports.append((stage, stats))


class FakeStageControl:
    def __init__(self):
        self.consumed = []

    def is_skip_requested(self, _stage):
        return False

    def consume_skip(self, stage):
        self.consumed.append(stage)
        return False


class FakeProcessing:
    def process_pending_with_stats(self, **_kwargs):
        return {"found": 0, "processed": 0, "failed": 0, "stopped": False}


class DisabledClassification:
    def process_pending_with_stats(self, **_kwargs):
        return {
            "found": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "stopped": False,
            "disabled": True,
        }


class FakeAdvertioService:
    def __init__(self):
        self.calls = []

    def deliver_pending(self, **kwargs):
        self.calls.append(kwargs)
        return {"found": 2, "sent": 1, "already_existed": 0, "failed": 1}


class FakeAIProcessing:
    def __init__(self, advertio_service):
        self.advertio_service = advertio_service


@pytest.mark.asyncio
async def test_scheduler_runs_advertio_retry_worker_even_when_ai_stage_is_disabled(monkeypatch):
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", True)
    advertio = FakeAdvertioService()
    scheduler = SchedulerService(
        crawl_job_service=object(),
        processing_service=FakeProcessing(),
        classification_service=DisabledClassification(),
        ai_processing_service=FakeAIProcessing(advertio),
    )
    scheduler.monitor = FakeMonitor()
    scheduler.stage_control = FakeStageControl()

    await scheduler._run_post_crawl_pipeline(
        client=object(),
        channel_username="all channels",
        crawl_result={"status": "completed"},
    )

    assert len(advertio.calls) == 1
    call = advertio.calls[0]
    assert call["limit"] == 100
    assert call["progress"] is True
    assert callable(call["media_downloader"])
    assert datetime.fromisoformat(call["before_datetime"]).tzinfo is not None
    assert "advertio" in scheduler.stage_control.consumed
    assert any(stage == "advertio" for stage, _stats in scheduler.monitor.reports)
