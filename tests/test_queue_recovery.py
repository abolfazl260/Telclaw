from datetime import datetime, timedelta, timezone

import config
from storage import database


def _row(message_id):
    with database.get_connection() as conn:
        return dict(conn.execute(
            "SELECT * FROM messages WHERE message_id=? AND channel_username=?",
            (message_id, "queue_fixture"),
        ).fetchone())


def test_startup_recovers_stale_stage_claims_but_keeps_fresh_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "queue.sqlite3"))
    monkeypatch.setattr(config, "QUEUE_PROCESSING_TIMEOUT_SECONDS", 3600)
    database.initialize_db()

    database.insert_message("queue_fixture", 1, "processing", "2026-10-01", processing_status="processing")
    database.insert_message("queue_fixture", 2, "classification", "2026-10-01", processing_status="processed")
    database.insert_message("queue_fixture", 3, "ai", "2026-10-01", processing_status="processed")
    database.insert_message("queue_fixture", 4, "fresh", "2026-10-01", processing_status="processing")

    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE messages SET processing_started_at=? WHERE message_id=1",
            (stale,),
        )
        conn.execute(
            """UPDATE messages
               SET classification_status='processing',
                   classification_started_at=?
               WHERE message_id=2""",
            (stale,),
        )
        conn.execute(
            """UPDATE messages
               SET classification_status='processed',
                   classification_category='joblist',
                   ai_status='processing',
                   ai_started_at=?
               WHERE message_id=3""",
            (stale,),
        )
        conn.execute(
            "UPDATE messages SET processing_started_at=? WHERE message_id=4",
            (fresh,),
        )

    database.initialize_db()

    assert _row(1)["processing_status"] == "pending"
    assert _row(1)["processing_started_at"] is None
    assert _row(2)["classification_status"] == "pending"
    assert _row(2)["classification_started_at"] is None
    assert _row(3)["ai_status"] == "pending"
    assert _row(3)["ai_started_at"] is None
    assert _row(4)["processing_status"] == "processing"


def test_stage_claim_is_atomic_and_records_start_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "claim.sqlite3"))
    database.initialize_db()
    database.insert_message("queue_fixture", 10, "pending", "2026-10-01")

    assert database.claim_processing_message(10, "queue_fixture") is True
    assert database.claim_processing_message(10, "queue_fixture") is False

    row = _row(10)
    assert row["processing_status"] == "processing"
    assert row["processing_started_at"]
