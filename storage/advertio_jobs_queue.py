"""Additive Jobs outbox schema and shared Advertio request accounting.

Called by database.initialize_db at startup, never by a crawl/channel iteration.
"""

from datetime import datetime, timezone


def initialize_jobs_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS advertio_jobs_deliveries (
            processed_message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            source_name TEXT NOT NULL,
            external_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            lead_id TEXT,
            remote_status TEXT,
            error_code TEXT,
            safety_flags TEXT,
            validated_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            first_attempt_at TEXT,
            last_attempt_at TEXT,
            updated_at TEXT NOT NULL,
            worker_token TEXT,
            UNIQUE(source_name, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_delivery_pending
            ON advertio_jobs_deliveries(status,next_attempt_at,processed_message_id);
        CREATE TABLE IF NOT EXISTS advertio_jobs_attempts (
            id INTEGER PRIMARY KEY,
            processed_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            attempted_at TEXT NOT NULL,
            finished_at TEXT,
            http_status INTEGER,
            status TEXT NOT NULL DEFAULT 'sending',
            lead_id TEXT,
            remote_status TEXT,
            error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_attempt_date ON advertio_jobs_attempts(attempted_at);
        CREATE TABLE IF NOT EXISTS advertio_jobs_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS advertio_request_log (
            id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_advertio_request_day ON advertio_request_log(attempted_at,category);
    """)
    # Freeze the deployment boundary ONCE, including when Jobs is disabled.
    # Old AI rows do not silently turn into a historical publishing campaign.
    conn.execute("""INSERT OR IGNORE INTO advertio_jobs_meta(key,value)
        SELECT 'deployment_watermark',CAST(COALESCE(MAX(id),0) AS TEXT) FROM messages""")
    conn.execute(
        "INSERT OR IGNORE INTO advertio_jobs_meta(key,value) VALUES('installed_at',?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
