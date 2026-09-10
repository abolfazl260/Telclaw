"""SQLite tracking and Telegram delivery helpers for transfer-list ads."""

from datetime import datetime, timezone

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
    """Return mutually exclusive total/sent/waiting/failed transfer counts."""
    initialize_transfer_delivery_table()
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM transferlist").fetchone()["n"] or 0
        sent = conn.execute(
            """SELECT COUNT(DISTINCT t.processed_message_id) AS n
               FROM transferlist t
               INNER JOIN telegram_transfer_delivery d
                 ON d.processed_message_id = t.processed_message_id
              WHERE d.status = 'sent'"""
        ).fetchone()["n"] or 0
        failed = conn.execute(
            """SELECT COUNT(DISTINCT t.processed_message_id) AS n
               FROM transferlist t
               INNER JOIN telegram_transfer_delivery d
                 ON d.processed_message_id = t.processed_message_id
              WHERE d.status = 'failed'
                AND NOT EXISTS (
                    SELECT 1 FROM telegram_transfer_delivery ds
                     WHERE ds.processed_message_id = t.processed_message_id
                       AND ds.status = 'sent'
                )"""
        ).fetchone()["n"] or 0
        waiting = max(int(total) - int(sent) - int(failed), 0)
        return {
            "total": int(total),
            "sent": int(sent),
            "waiting": waiting,
            "failed": int(failed),
        }
    finally:
        conn.close()


def _delivery_status_for_message(conn, processed_message_id):
    row = conn.execute(
        """SELECT
             MAX(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
             MAX(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
           FROM telegram_transfer_delivery
          WHERE processed_message_id=?""",
        (int(processed_message_id),),
    ).fetchone()
    if row["sent"]:
        return "sent"
    if row["failed"]:
        return "failed"
    return "waiting"


def get_ready_transfer_ads(limit=100):
    """Return processed transfer records that have not been successfully delivered."""
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
                      SELECT 1 FROM telegram_transfer_delivery d
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


def get_latest_transfer_ads(limit=20):
    """Return the latest transfer records with their current delivery status."""
    initialize_transfer_delivery_table()
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT t.*, m.channel_username, m.message_id, m.message_link
                 FROM transferlist t
                 INNER JOIN messages m ON m.id = t.processed_message_id
                ORDER BY t.id DESC
                LIMIT ?""",
            (int(limit),),
        ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["delivery_status"] = _delivery_status_for_message(
                conn, record["processed_message_id"]
            )
            records.append(record)
        return records
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


def format_transfer_ad(record):
    """Build a Telegram-safe text representation from extracted transfer fields."""
    origin = record.get("origin_city") or record.get("origin_country") or "Unknown"
    destination = record.get("destination_city") or record.get("destination_country") or "Unknown"
    title = record.get("title") or f"Transfer: {origin} → {destination}"

    lines = [f"✈️ {title}", ""]
    lines.append(f"📍 Route: {origin} → {destination}")
    if record.get("transport_type"):
        lines.append(f"🚚 Transport: {record['transport_type']}")
    if record.get("cargo_type"):
        lines.append(f"📦 Cargo: {record['cargo_type']}")
    if record.get("weight") is not None:
        weight = record["weight"]
        unit = record.get("weight_unit") or ""
        lines.append(f"⚖️ Weight: {weight} {unit}".rstrip())
    if record.get("quantity") is not None:
        lines.append(f"🔢 Quantity: {record['quantity']}")
    if record.get("airline"):
        lines.append(f"✈️ Airline: {record['airline']}")
    if record.get("flight_number"):
        lines.append(f"🎫 Flight: {record['flight_number']}")
    if record.get("departure_date") or record.get("departure_time"):
        lines.append(
            f"🛫 Departure: {record.get('departure_date') or ''} "
            f"{record.get('departure_time') or ''}".strip()
        )
    if record.get("arrival_date") or record.get("arrival_time"):
        lines.append(
            f"🛬 Arrival: {record.get('arrival_date') or ''} "
            f"{record.get('arrival_time') or ''}".strip()
        )
    if record.get("price") is not None:
        currency = record.get("currency") or ""
        lines.append(f"💰 Price: {record['price']} {currency}".rstrip())
    if record.get("contact"):
        lines.append(f"📞 Contact: {record['contact']}")
    if record.get("description"):
        lines.extend(["", record["description"]])
    if record.get("features"):
        lines.append(f"⭐ Features: {record['features']}")
    if record.get("message_link"):
        lines.extend(["", f"🔗 Source: {record['message_link']}"])
    return "\n".join(lines)


async def send_transfer_ads(client, target_channel, limit=20):
    """Send unsent transfer ads to one Telegram channel and persist each result."""
    records = get_ready_transfer_ads(limit=limit)
    result = {"found": len(records), "sent": 0, "failed": 0}
    if not records:
        return result

    for record in records:
        message_id = record["processed_message_id"]
        try:
            sent = await client.send_message(target_channel, format_transfer_ad(record))
            sent_id = getattr(sent, "id", None)
            record_transfer_delivery(
                message_id,
                target_channel,
                "sent",
                sent_message_id=sent_id,
                sent_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
            result["sent"] += 1
        except Exception as exc:
            record_transfer_delivery(
                message_id,
                target_channel,
                "failed",
                sent_message_id=None,
                sent_at=None,
                error=str(exc),
            )
            result["failed"] += 1
    return result
