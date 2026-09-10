"""SQLite tracking and Telegram delivery helpers for transfer-list ads."""

from datetime import datetime, timezone
import re

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
        return {"total": int(total), "sent": int(sent), "waiting": waiting, "failed": int(failed)}
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
            """SELECT t.*, m.channel_username, m.message_id, m.message_link, m.sender_username
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
            """SELECT t.*, m.channel_username, m.message_id, m.message_link, m.sender_username
                 FROM transferlist t
                 INNER JOIN messages m ON m.id = t.processed_message_id
                ORDER BY t.id DESC
                LIMIT ?""",
            (int(limit),),
        ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["delivery_status"] = _delivery_status_for_message(conn, record["processed_message_id"])
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
            (int(processed_message_id), str(target_channel), str(status), sent_message_id, sent_at, error),
        )
        conn.commit()
    finally:
        conn.close()


def _format_number(value):
    if value is None:
        return ""
    try:
        number = float(value)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return str(value)


def _gregorian_to_jalali(gy, gm, gd):
    """Convert Gregorian date to Solar Hijri/Jalali without external dependencies."""
    g_days_in_month = [31, 29 if (gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gy2 = gy - 1600
    jy = 979
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += g_days_in_month[i]
    g_day_no += gd2
    j_day_no = g_day_no - 79
    jy += 33 * (j_day_no // 12053)
    j_day_no %= 12053
    jy += 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    if j_day_no < 186:
        jm = 1 + j_day_no // 31
        jd = 1 + j_day_no % 31
    else:
        jm = 7 + (j_day_no - 186) // 30
        jd = 1 + (j_day_no - 186) % 30
    return jy, jm, jd


def _format_departure_date(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        return text
    gy, gm, gd = map(int, match.groups())
    try:
        jy, jm, jd = _gregorian_to_jalali(gy, gm, gd)
        return f"{gy:04d}-{gm:02d}-{gd:02d} ({jy:04d}/{jm:02d}/{jd:02d})"
    except ValueError:
        return text


def _mask_contact(value):
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and digits.startswith("1") and len(digits) >= 11:
        return "+1" + "X" * (len(digits) - 1)
    if len(digits) >= 8:
        return "X" * len(digits)
    return text


def _format_transport(value):
    text = str(value or "").strip()
    if not text:
        return ""
    key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    mapping = {
        "air": "هوایی", "air cargo": "هوایی", "air freight": "هوایی",
        "airline": "هوایی", "sea": "دریایی", "ocean": "دریایی",
        "sea freight": "دریایی", "land": "زمینی", "ground": "زمینی",
        "road": "زمینی", "truck": "زمینی", "rail": "ریلی", "train": "ریلی",
    }
    return mapping.get(key, text)


def format_transfer_ad(record):
    """Build the requested Persian transfer-ad format for Telegram."""
    origin = str(record.get("origin_city") or "نامشخص").strip()
    destination = str(record.get("destination_city") or "نامشخص").strip()

    lines = [f"🚚 ارسال بار از {origin} به {destination}"]

    if record.get("cargo_type"):
        lines.append(f"📦 نوع بار: {record['cargo_type']}")
    if record.get("weight") is not None:
        weight = _format_number(record["weight"])
        unit = str(record.get("weight_unit") or "").strip()
        lines.append(f"⚖️ وزن: {weight}{(' ' + unit) if unit else ''}")
    if record.get("volume") is not None:
        volume = _format_number(record["volume"])
        unit = str(record.get("volume_unit") or "m³").strip()
        lines.append(f"📏 حجم: {volume} {unit}")
    if record.get("departure_date"):
        lines.append(f"📅 تاریخ ارسال: {_format_departure_date(record['departure_date'])}")
    if record.get("transport_type"):
        lines.append(f"🚛 نوع حمل: {_format_transport(record['transport_type'])}")
    if record.get("price") is not None:
        lines.append(f"💰 هزینه: {_format_number(record['price'])}")
    if record.get("contact"):
        lines.append(f"📞 تماس: {_mask_contact(record['contact'])}")
    username = str(record.get("sender_username") or "").strip()
    if username:
        if not username.startswith("@"):
            username = "@" + username
        lines.append(f"یوزرنیم: {username}")

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
                message_id, target_channel, "sent", sent_message_id=sent_id,
                sent_at=datetime.now(timezone.utc).isoformat(), error=None,
            )
            result["sent"] += 1
        except Exception as exc:
            record_transfer_delivery(
                message_id, target_channel, "failed", sent_message_id=None,
                sent_at=None, error=str(exc),
            )
            result["failed"] += 1
    return result
