import json

import pytest

import backoffice_health
import backoffice_web
import config
from monitoring.telegram_monitor import TelegramMonitor
from storage import database


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    db_path = tmp_path / "health.db"
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps({
        "transport": [
            {"username": "crawled_channel", "name": "Crawled source"},
            {"username": "not_crawled_yet", "name": "Waiting source"},
        ]
    }), encoding="utf-8")
    monkeypatch.setattr(config, "DB_NAME", str(db_path))
    monkeypatch.setattr(config, "CHANNELS_JSON", str(channels_path))
    database.initialize_db()
    conn = database.get_connection()
    conn.execute("""INSERT INTO messages(
        channel_username,message_id,text,date,collection_status,processing_status,
        classification_status,classification_category,ai_status,ai_category,
        advertio_status,channel_name,cleaned_at,classification_processed_at,ai_processed_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("crawled_channel", 101, "one", "2026-09-28T08:00:00+00:00",
         "collected", "processed", "processed", "transferlist", "processed",
         "transferlist", "waiting", "Crawled Channel",
         "2026-10-02T10:00:00+00:00", "2026-10-02T10:01:00+00:00",
         "2026-10-02T10:02:00+00:00"))
    conn.execute("""INSERT INTO messages(
        channel_username,message_id,text,date,collection_status,processing_status,
        classification_status,ai_status,advertio_status,channel_name
    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("crawled_channel", 102, "two", "2026-09-28T09:00:00+00:00",
         "collected", "pending", "pending", "waiting", "waiting", "Crawled Channel"))
    conn.commit()
    conn.close()
    return db_path


def test_health_snapshot_reports_database_crawls_and_channels(health_db):
    report = backoffice_health.snapshot()
    assert report["database"]["healthy"] is True
    assert report["database"]["row_counts"]["messages"] == 2
    assert report["pipeline"]["total"] == 2
    assert report["pipeline"]["processing_pending"] == 1
    assert report["pipeline"]["last_processing"] == "2026-10-02T10:00:00+00:00"
    assert report["pipeline"]["last_classification"] == "2026-10-02T10:01:00+00:00"
    assert report["pipeline"]["last_ai"] == "2026-10-02T10:02:00+00:00"
    assert report["daily"][0]["day"] == "2026-09-28"
    assert report["daily"][0]["crawled"] == 2
    assert report["stage_daily"][0]["day"] == "2026-10-02"
    assert report["stage_daily"][0]["processed"] == 1
    assert report["stage_daily"][0]["classified"] == 1
    assert report["stage_daily"][0]["ai_processed"] == 1

    channels = {row["channel_username"]: row for row in report["channels"]}
    assert channels["crawled_channel"]["messages"] == 2
    assert channels["crawled_channel"]["crawl_state"] == "crawled"
    assert channels["not_crawled_yet"]["messages"] == 0
    assert channels["not_crawled_yet"]["crawl_state"] == "not crawled"


@pytest.mark.asyncio
async def test_robot_reports_are_persisted_and_rendered_in_health_tab(health_db):
    monitor = TelegramMonitor()
    monitor.enabled = False
    await monitor.report("crawl", {"channel": "@crawled_channel", "saved": 2, "status": "completed"})
    await monitor.error("ERROR", "test.health", "sample failure")

    report = backoffice_health.snapshot()
    kinds = [row["kind"] for row in report["activity"]]
    assert "crawl" in kinds
    assert "error" in kinds
    crawl = next(row for row in report["activity"] if row["kind"] == "crawl")
    assert crawl["details_data"]["saved"] == 2

    response = await backoffice_web.health_page({})
    assert response.status == 200
    assert "System Health" in response.text
    assert "Pipeline activity · by stage time" in response.text
    assert "Message cohort status · by message date" in response.text
    assert "Crawled channels" in response.text
    assert "Robot &amp; system activity" in response.text
    assert "@crawled_channel" in response.text
    assert "@not_crawled_yet" in response.text
    assert "sample failure" in response.text


@pytest.mark.asyncio
async def test_backoffice_navigation_contains_health_tab(health_db):
    # The health endpoint is an authenticated route just like publishing and database.
    app = backoffice_web.create_app()
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/health" in paths
