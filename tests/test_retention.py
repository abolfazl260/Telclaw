import logging
import sqlite3
from datetime import datetime, timezone

from services.retention import ensure_archive_view, prune_activity, prune_log_files


NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)


def test_prune_rotations_only(tmp_path):
    base = tmp_path / "errors.log"
    names = [
        "errors.log", "errors.log.2026-10-01_00",
        "errors.log.info.2026-11-01_00", "errors.log.debug.2026-11-20_00",
        "errors.log.debug.2026-11-22_00", "errors.log.info.2026-11-12_00",
        "errors.log.2026-11-01_00", "unrelated.log.2026-01-01_00",
        "errors.log.debug.notes",
    ]
    for name in names:
        (tmp_path / name).write_text("test")
    assert prune_log_files(base, NOW) == 3
    assert {p.name for p in tmp_path.iterdir()} == set(names) - set(names[1:4])


def test_activity_retention_keeps_stage_checkpoint_and_recent_data():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE system_activity (id INTEGER PRIMARY KEY, kind TEXT, level TEXT, created_at TEXT)")
    rows = [
        (1, "debug", "DEBUG", "2026-11-20T00:00:00+00:00"),
        (2, "heartbeat", "INFO", "2026-11-20T00:00:00+00:00"),
        (3, "info", "INFO", "2026-11-01T00:00:00+00:00"),
        (4, "error", "ERROR", "2026-10-01T00:00:00+00:00"),
        (5, "warning", "WARNING", "2026-11-01T00:00:00+00:00"),
        (6, "crawl", "INFO", "2026-10-01T00:00:00+00:00"),
        (7, "crawl", "INFO", "2026-10-02T00:00:00+00:00"),
        (8, "debug", "DEBUG", "2026-11-22T12:00:00+00:00"),
        (9, "info", "INFO", "2026-11-11T00:00:00+00:00"),
    ]
    conn.executemany("INSERT INTO system_activity VALUES(?,?,?,?)", rows)
    assert prune_activity(conn, NOW) == 5
    assert [r[0] for r in conn.execute("SELECT id FROM system_activity ORDER BY id")] == [5, 7, 8, 9]
    assert prune_activity(conn, NOW) == 0


def test_archive_is_logical_and_excludes_unknown_dates():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE messages(id INTEGER PRIMARY KEY, channel_username TEXT, message_id INTEGER);
        CREATE TABLE transferlist(processed_message_id INTEGER, departure_date TEXT);
        INSERT INTO messages VALUES(1,'a',11),(2,'a',12),(3,'b',13);
        INSERT INTO transferlist VALUES(1,'2000-01-01'),(2,'2999-01-01'),(3,NULL);
    """)
    ensure_archive_view(conn)
    ensure_archive_view(conn)
    assert conn.execute("SELECT source_telegram_message_id FROM archived_expired_cargo").fetchall() == [(11,)]
    assert conn.execute("SELECT COUNT(*) FROM transferlist").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (3,)
