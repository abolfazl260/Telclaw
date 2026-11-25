"""Bounded operational retention; never delete source listings or delivery state."""
import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import threading

LOG = logging.getLogger(__name__)
DEBUG_AGE = timedelta(hours=84)
INFO_AGE = timedelta(days=15)
ALERT_AGE = timedelta(days=45)
HEARTBEAT_AGE = timedelta(hours=84)
INTERVAL = timedelta(hours=12)
BATCH_SIZE = 500
MAX_BATCHES = 10
_LOCK = threading.Lock()


class _LevelBand(logging.Filter):
    def __init__(self, low, high):
        super().__init__()
        self.low, self.high = low, high

    def filter(self, record):
        return self.low <= record.levelno < self.high


def configure_logging(error_path):
    """Install once without removing Telegram/console or test handlers.

    Preserve the previous default root threshold (ERROR). INFO/DEBUG records
    enabled by individual loggers or deployment logging configuration are
    retained separately, without globally increasing logging volume.
    """
    root = logging.getLogger()
    with _LOCK:
        if any(getattr(h, "_telclaw_retention", False) for h in root.handlers):
            return
        if not root.handlers:
            root.setLevel(logging.ERROR)
        for path, low, high, age in _bands(error_path):
            handler = TimedRotatingFileHandler(
                path, when="H", interval=12, backupCount=0,
                encoding="utf-8", utc=True, delay=True,
            )
            handler.setLevel(low)
            handler.addFilter(_LevelBand(low, high))
            handler.setFormatter(logging.Formatter(
                "%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
            ))
            handler._telclaw_retention = True
            handler.retention_age = age
            root.addHandler(handler)


def _bands(error_path):
    path = Path(error_path).absolute()
    return (
        (Path(str(path) + ".debug"), logging.DEBUG, logging.INFO, DEBUG_AGE),
        (Path(str(path) + ".info"), logging.INFO, logging.WARNING, INFO_AGE),
        (path, logging.WARNING, float("inf"), ALERT_AGE),
    )


def prune_log_files(error_path, now=None):
    """Remove only our own rotated files whose entire interval has expired.

    Roll inactive managed files too so a quiet service does not retain an old
    active log forever. Never delete an active log or an unrelated file.
    """
    now = now or datetime.now(timezone.utc)
    bases = {str(p) for p, _, _, _ in _bands(error_path)}
    for handler in logging.getLogger().handlers:
        if (getattr(handler, "_telclaw_retention", False)
                and handler.baseFilename in bases):
            handler.acquire()
            try:
                if handler.rolloverAt <= now.timestamp():
                    handler.doRollover()
            finally:
                handler.release()
    removed = 0
    for base, _, _, age in _bands(error_path):
        pattern = re.compile(re.escape(base.name) + r"\.(\d{4}-\d{2}-\d{2}_\d{2})$")
        if not base.parent.is_dir():
            continue
        for candidate in base.parent.iterdir():
            match = pattern.fullmatch(candidate.name)
            if not match or candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                start = datetime.strptime(match.group(1), "%Y-%m-%d_%H").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if start + INTERVAL <= now - age:
                candidate.unlink(missing_ok=True)
                removed += 1
    return removed


def prune_activity(conn, now=None, *, max_batches=MAX_BATCHES):
    """Delete bounded batches of old activity, not messages/queue/audit data.

    Keep the newest non-log event of each kind for stage health even when old.
    Timestamps written by record_system_activity are UTC ISO 8601.
    """
    now = now or datetime.now(timezone.utc)
    policies = (
        ("kind IN ('heartbeat','worker_heartbeat')", HEARTBEAT_AGE, False),
        ("kind NOT IN ('heartbeat','worker_heartbeat') AND level='DEBUG'", DEBUG_AGE, True),
        ("kind NOT IN ('heartbeat','worker_heartbeat') AND level='INFO'", INFO_AGE, True),
        ("kind NOT IN ('heartbeat','worker_heartbeat') AND level IN ('WARN','WARNING','ERROR','CRITICAL')", ALERT_AGE, False),
    )
    total = 0
    for predicate, age, preserve in policies:
        keep = """
            AND (kind IN ('debug','info','warning','error') OR id <> (
                SELECT a2.id FROM system_activity a2
                 WHERE a2.kind=system_activity.kind
                 ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1
            ))
        """ if preserve else ""
        for _ in range(max_batches):
            with conn:
                cur = conn.execute(
                    f"""DELETE FROM system_activity WHERE id IN (
                        SELECT id FROM system_activity WHERE created_at < ?
                          AND ({predicate}) {keep}
                        ORDER BY created_at, id LIMIT ?
                    )""",
                    ((now - age).isoformat(), BATCH_SIZE),
                )
            total += cur.rowcount
            if cur.rowcount < BATCH_SIZE:
                break
    return total


def ensure_archive_view(conn):
    """Logical archive of explicitly expired cargo, preserving all identities.

    Housing/jobs have no reliable expiry field. Unknown dates are not archived.
    The view is read-only, updates automatically and does not alter delivery.
    """
    with conn:
        conn.execute("""
            CREATE VIEW IF NOT EXISTS archived_expired_cargo AS
            SELECT t.*, m.channel_username AS source_channel_username,
                   m.message_id AS source_telegram_message_id
              FROM transferlist t JOIN messages m ON m.id=t.processed_message_id
             WHERE date(substr(t.departure_date,1,10)) < date('now')
        """)


def maintain():
    import config
    from storage.database import get_connection
    removed = prune_log_files(config.ERROR_LOG_FILE)
    with closing(get_connection()) as conn:
        ensure_archive_view(conn)
        activity = prune_activity(conn)
    return {"rotated_logs_removed": removed, "activity_rows_removed": activity}


async def run_maintenance():
    # Startup grace period; at most one maintenance pass every 12 hours.
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(maintain)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Operational retention maintenance failed")
        await asyncio.sleep(INTERVAL.total_seconds())
