"""SQLite tracking helpers for Telegram transfer-list deliveries."""

from storage.database import get_connection


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS telegram_transfer_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    processed_message_id INTEGER NOT NULL,
    target_channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    sent_message_id INTEGER,
    sent_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(processed_message_id, target_channel),
    FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE
)
"""


def initialize_transfer_delivery_table():
    conn = get_connection()
    try:
        conn.execute(TABLE_SQL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transfer_delivery_status "
            "ON telegram_transfer_delivery(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transfer_delivery_message "
            "ON telegram_transfer_delivery(processed_message_id)"
        )
        conn.commit()
    finally:
        conn.close()


def get_transfer_queue_status():
    """Return total, already-sent and ready transfer-list counts."""
    initialize_transfer_delivery_table()
    conn = get_connection()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM transferlist"
        ).fetchone()["n"] or 0

        sent = conn.execute(
            """SELECT COUNT(DISTINCT t.processed_message_id) AS n
               FROM transferlist t
               INNER JOIN telegram_transfer_delivery d
                 ON d.processed_message_id = t.processed_message_id
              WHERE d.status = 'sent'"""
        ).fetchone()["n"] or 0

        ready = conn.execute(
            """SELECT COUNT(*) AS n
               FROM transferlist t
               INNER JOIN messages m ON m.id = t.processed_message_id
              WHERE m.processing_status = 'processed'
                AND m.ai_status = 'processed'
                AND m.ai_category = 'transferlist'
                AND NOT EXISTS (
                    SELECT 1
                      FROM telegram_transfer_delivery d
                     WHERE d.processed_message_id = t.processed_message_id
                       AND d.status = 'sent'
                )"""
        ).fetchone()["n"] or 0

        return {
            "total": int(total),
            "sent": int(sent),
            "ready": int(ready),
        }
    finally:
        conn.close()


def get_ready_transfer_ads(limit=100):
    """Return transfer records that are processed and have not been sent successfully."""
    initialize_transfer_delivery_table()
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT t.*, m.channel_username, m.message_id, m.message_link
                 FROM transferlist t
                 INNER JOIN messages m ON m.id = t.processed_message_id
                WHERE m.processing_status = 'processed'
                  AND m.ai_status = 'processed'
                  AND m.ai_category = 'transferlist'
                  AND NOT EXISTS (
                      SELECT 1
                        FROM telegram_transfer_delivery d
                       WHERE d.processed_message_id = t.processed_message_id
                         AND d.status = 'sent'
                  )
                ORDER BY t.id ASC
                LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def record_transfer_delivery(processed_message_id, target_channel, status,
                              sent_message_id=None, sent_at=None, error=None):
    """Insert/update one Telegram transfer delivery attempt."""
    initialize_transfer_delivery_table()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO telegram_transfer_delivery
                   (processed_message_id, target_channel, status,
                    sent_message_id, sent_at, error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(processed_message_id, target_channel) DO UPDATE SET
                    status=excluded.status,
                    sent_message_id=excluded.sent_message_id,
                    sent_at=excluded.sent_at,
                    error=excluded.error,
                    updated_at=CURRENT_TIMESTAMP""",
            (
                int(processed_message_id),
                str(target_channel),
                str(status),
                sent_message_id,
                sent_at,
                error,
            ),
        )
        conn.commit()
    finally:
        conn.close()
