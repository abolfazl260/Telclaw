"""Durable cursors for scheduled Telegram collection; never infer from stored ads."""
from datetime import date, datetime, timedelta, timezone
import hashlib

RECONCILE_INTERVAL = timedelta(hours=24)
RECONCILE_DAYS = 2


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crawl_checkpoints (
        channel_username TEXT NOT NULL, peer_id INTEGER NOT NULL,
        crawl_mode TEXT NOT NULL, start_date TEXT NOT NULL,
        last_message_id INTEGER NOT NULL, last_success_at TEXT NOT NULL,
        last_reconcile_at TEXT NOT NULL, last_new_count INTEGER NOT NULL,
        last_scan_count INTEGER NOT NULL, last_scan_mode TEXT NOT NULL,
        PRIMARY KEY(channel_username, peer_id, crawl_mode, start_date)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS crawl_source_edits (
        channel_username TEXT NOT NULL, peer_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL, edit_date TEXT NOT NULL,
        content_hash TEXT NOT NULL, detected_at TEXT NOT NULL,
        PRIMARY KEY(channel_username, peer_id, message_id, edit_date, content_hash)
    )""")


def plan(conn, channel, peer_id, mode, floor, end, now=None):
    """Return an exact-scope checkpoint, lower ID, and effective date floor.

    A fresh scope does a full baseline. Reconciliation revisits the recent
    window and any interval since last successful scan, including downtime.
    Historical ranges do not participate in checkpoints.
    """
    now = now or datetime.now(timezone.utc)
    if not peer_id or end != date.today():
        return None, 0, floor, "historical"
    row = conn.execute("""SELECT * FROM crawl_checkpoints
        WHERE channel_username=? AND peer_id=? AND crawl_mode=? AND start_date=?""",
        (channel, peer_id, mode, floor.isoformat())).fetchone()
    if row is None:
        return None, 0, floor, "baseline"
    previous = dict(row)
    last = datetime.fromisoformat(previous["last_success_at"])
    reconciled = datetime.fromisoformat(previous["last_reconcile_at"])
    # The two-day overlap protects day-boundary transitions and recent edits.
    # A missed day must not strand new IDs behind a date filter.
    recent = end - timedelta(days=RECONCILE_DAYS - 1)
    catchup = max(floor, min(recent, last.date() - timedelta(days=1)))
    if now - reconciled >= RECONCILE_INTERVAL:
        return previous, 0, catchup, "reconcile"
    return previous, previous["last_message_id"], floor, "incremental"


def save(conn, channel, peer_id, mode, floor, previous, newest, scanned, new_count,
         scan_mode, now=None):
    """Advance only after the caller has completed every in-scope message."""
    now = now or datetime.now(timezone.utc)
    last_id = max(newest or 0, (previous or {}).get("last_message_id", 0))
    if not last_id:
        return
    stamp = now.isoformat()
    reconciled = (previous or {}).get("last_reconcile_at", stamp) if scan_mode == "incremental" else stamp
    with conn:
        conn.execute("""INSERT INTO crawl_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(channel_username,peer_id,crawl_mode,start_date) DO UPDATE SET
              last_message_id=MAX(crawl_checkpoints.last_message_id,excluded.last_message_id),
              last_success_at=excluded.last_success_at,
              last_reconcile_at=MAX(crawl_checkpoints.last_reconcile_at,excluded.last_reconcile_at),
              last_new_count=excluded.last_new_count,last_scan_count=excluded.last_scan_count,
              last_scan_mode=excluded.last_scan_mode
            WHERE excluded.last_success_at >= crawl_checkpoints.last_success_at""",
            (channel, peer_id, mode, floor.isoformat(), last_id, stamp, reconciled,
             new_count, scanned, scan_mode))


def record_edit(conn, channel, peer_id, message_id, edit_date, text):
    """Record changed Telegram text fingerprint, not a raw-text overwrite."""
    if not edit_date or not isinstance(text, str):
        return False
    row = conn.execute("""SELECT raw_text FROM messages
        WHERE channel_username=? AND message_id=? AND channel_id=?""",
        (channel, message_id, peer_id)).fetchone()
    if not row or row[0] is None or row[0] == text:
        return False
    with conn:
        cur = conn.execute("""INSERT OR IGNORE INTO crawl_source_edits
            VALUES(?,?,?,?,?,?)""", (channel, peer_id, message_id, edit_date.isoformat(),
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            datetime.now(timezone.utc).isoformat()))
    return cur.rowcount == 1
