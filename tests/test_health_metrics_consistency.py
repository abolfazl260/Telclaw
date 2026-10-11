"""Regression tests for unified /health, Back Office and terminal queue metrics."""
from datetime import datetime, timezone

import pytest

import backoffice_health
import config
from monitoring.telegram_monitor import TelegramMonitor
from services.health_metrics import collect
from storage import database


@pytest.fixture
def health_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "health-metrics.db"))
    monkeypatch.setattr(config, "AI_CLASSIFICATION_ENABLED", True)
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", True)
    monkeypatch.setattr(config, "AI_CLASSIFICATION_MAX_RETRIES", 3)
    database.initialize_db()
    with database.get_connection() as conn:
        for i in range(1, 11):
            conn.execute(
                "INSERT INTO messages(channel_username,message_id,text,date,processing_status,"
                "classification_status,ai_status,ai_category,classification_attempts,advertio_status) "
                "VALUES(?,?,?,'2026-10-01','pending','waiting','waiting',NULL,0,'waiting')",
                ("source", i, f"Test message {i}"),
            )
        def set_fields(i, **fields):
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE messages SET {sets} WHERE message_id=?",
                (*fields.values(), i),
            )
        set_fields(2, processing_status="processed", classification_status="pending")
        set_fields(3, processing_status="processed", classification_status="failed",
                   classification_attempts=1)
        set_fields(4, processing_status="processed", classification_status="processed",
                   classification_category="housinglist", ai_category="housinglist", ai_status="pending")
        set_fields(5, processing_status="processed", classification_status="processed",
                   classification_category="joblist", ai_category="joblist", ai_status="processed")
        set_fields(6, processing_status="processed", classification_status="processed",
                   classification_category="transferlist", ai_category="transferlist", ai_status="processed")
        set_fields(7, processing_status="processed", classification_status="processed",
                   classification_category="housinglist", ai_category="housinglist", ai_status="processed")
        set_fields(8, processing_status="processed", classification_status="processed",
                   classification_category="joblist", ai_category="joblist", ai_status="failed")
        set_fields(9, processing_status="processed", classification_status="processed",
                   classification_category="housinglist", ai_category="housinglist",
                   ai_status="processed", advertio_status="rejected")
        set_fields(10, processing_status="failed", ai_status="failed")
        # A housing record must actually exist to be eligible for Advertio.
        for i in (7, 9):
            conn.execute(
                "INSERT INTO housinglist(processed_message_id,title) "
                "SELECT id, ? FROM messages WHERE message_id=?",
                (f"House {i}", i),
            )
        # All ten rows started pending. Only row 1 remains processing-pending.
        for i in (2,3,4,5,6,7,8,9,10):
            pass
        conn.commit()
    return None


@pytest.mark.asyncio
async def test_queue_metrics_exclude_nonhousing_advertio_and_count_unique_failed(health_data):
    with database.get_connection() as conn:
        result = collect(conn)
    assert result["processing_pending"] == 1
    assert result["classification_pending"] == 2
    assert result["ai_pending"] == 1
    assert result["advertio_pending"] == 1
    assert result["backlog"] == 5
    assert result["processing_failed"] == 1
    assert result["classification_failed"] == 1
    assert result["ai_failed"] == 2
    assert result["advertio_failed"] == 1
    assert result["failed"] == 4
    assert result["states"]["database"] == "HEALTHY"

    telegram = database.get_pipeline_health()
    admin = backoffice_health.snapshot()["pipeline"]
    status = database.get_pipeline_status()
    assert telegram["backlog"] == admin["backlog"] == 5
    assert telegram["failed"] == admin["failed"] == 4
    for stage in ("processing", "classification", "ai", "advertio"):
        assert telegram[f"{stage}_pending"] == admin[f"{stage}_pending"]
        assert status[f"{stage}_pending"] == admin[f"{stage}_pending"]
        assert telegram[f"{stage}_failed"] == admin[f"{stage}_failed"]
    assert telegram["crawler"] == "WARNING"

    monitor = TelegramMonitor()
    html = await monitor._build_health_message()
    assert "Pipeline backlog (unique messages):</b> 5" in html
    assert "Failed items (unique messages):</b> 4" in html
    assert "Last crawl:</b> Not recorded" in html
    assert "2026-10-01 03:30:00" not in html


@pytest.mark.asyncio
async def test_crawl_uses_actual_event_timestamp_not_message_date(health_data):
    when = datetime(2026, 10, 10, 8, 21, tzinfo=timezone.utc)
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO system_activity(kind,level,source,message,details,created_at)"
            "VALUES('crawl','INFO','test','Crawl completed',?,?)",
            ('{"status":"completed","saved":0}', when.isoformat()),
        )
        conn.commit()
        result = collect(conn, now=when)
    assert result["last_crawl"] == when.isoformat()
    assert result["states"]["crawler"] == "HEALTHY"
    assert result["states"]["database"] == "HEALTHY"
    assert result["last_processing"] is None
    assert database.get_pipeline_health()["last_crawl"] == when.isoformat()
    monitor = TelegramMonitor()
    html = await monitor._build_health_message()
    assert "2026-10-10 11:51:00 Tehran" in html
    assert "2026-10-01 03:30:00 Tehran" not in html


def test_stale_crawl_and_disabled_stages_show_explicit_health_states(health_data, monkeypatch):
    monkeypatch.setattr(config, "AI_CLASSIFICATION_ENABLED", False)
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", False)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", False)
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO system_activity(kind,level,source,message,details,created_at)"
            "VALUES('crawl','INFO','test','Old crawl',?,?)",
            ('{"status":"completed"}', "2026-10-07T00:00:00+00:00"),
        )
        conn.commit()
        result = collect(conn, now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert result["states"]["crawler"] == "WARNING"
    assert result["states"]["classification"] == "DISABLED"
    assert result["states"]["ai"] == "DISABLED"
    assert result["states"]["advertio"] == "DISABLED"


def test_failed_crawl_is_not_healthy_just_because_a_report_exists(health_data):
    when = datetime(2026, 10, 10, tzinfo=timezone.utc)
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO system_activity(kind,level,source,message,details,created_at)"
            "VALUES('crawl','INFO','test','Failed',?,?)",
            ('{"status":"failed"}', when.isoformat()),
        )
        conn.commit()
        result = collect(conn, now=when)
    assert result["states"]["crawler"] == "WARNING"


def test_historical_failures_do_not_trigger_critical_with_empty_active_queues(health_data):
    """A stale failed record is history, not evidence of a stalled live worker."""
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    old = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).isoformat()
    with database.get_connection() as conn:
        # Drain all actionable queues while retaining failed records intact.
        conn.execute("UPDATE messages SET processing_status='processed' WHERE message_id=1")
        conn.execute(
            "UPDATE messages SET classification_status='processed', ai_category='none', "
            "ai_status='skipped' WHERE message_id IN (2, 3)"
        )
        conn.execute("UPDATE messages SET ai_status='skipped' WHERE message_id=4")
        conn.execute("UPDATE messages SET advertio_status='sent' WHERE message_id=7")
        for kind in ("processing", "classification", "ai", "advertio"):
            conn.execute(
                "INSERT INTO system_activity(kind, level, created_at) "
                "VALUES(?, 'INFO', ?)", (kind, old),
            )
        conn.commit()
        result = collect(conn, now=now)

    assert result["backlog"] == 0
    assert result["processing_pending"] == 0
    assert result["classification_pending"] == 0
    assert result["ai_pending"] == 0
    assert result["advertio_pending"] == 0
    # Do not discard or rewrite actual failed statuses in persisted records.
    assert result["processing_failed"] == 1
    assert result["ai_failed"] == 2
    assert result["advertio_failed"] == 1
    assert result["failed"] == 4
    for stage in ("processing", "classification", "ai", "advertio"):
        assert result["states"][stage] == "WARNING", stage


def test_stale_stage_with_actual_pending_work_remains_critical(health_data):
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    old = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).isoformat()
    with database.get_connection() as conn:
        for kind in ("processing", "classification", "ai", "advertio"):
            conn.execute(
                "INSERT INTO system_activity(kind, level, created_at) "
                "VALUES(?, 'INFO', ?)", (kind, old),
            )
        conn.commit()
        result = collect(conn, now=now)

    assert result["backlog"] == 5
    for stage in ("processing", "classification", "ai", "advertio"):
        assert result["states"][stage] == "CRITICAL", stage


def test_advertio_health_eligibility_matches_the_actual_terminal_queue(health_data):
    """Partly processed rows must not inflate the live Advertio queue."""
    from storage.message_repository import MessageRepository

    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO messages(channel_username, message_id, text, date, "
            "collection_status, processing_status, classification_status, "
            "ai_status, ai_category, advertio_status) "
            "VALUES('source', 11, 'old', '2026-10-01', 'collected', 'pending', "
            "'processed', 'processed', 'housinglist', 'waiting')"
        )
        conn.execute(
            "INSERT INTO housinglist(processed_message_id, title) "
            "SELECT id, 'Not processed yet' FROM messages WHERE message_id=11"
        )
        conn.commit()
        result = collect(conn)

    eligible = MessageRepository().get_advertio_pending(limit=1000)
    assert result["advertio_pending"] == 1
    assert {r["message_id"] for r in eligible} == {7}
