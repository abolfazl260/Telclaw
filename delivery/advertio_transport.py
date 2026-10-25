"""Shared process concurrency and atomic, persistent daily HTTP reservations."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import BoundedSemaphore

import config
from storage.database import get_connection

_SLOTS = BoundedSemaphore(config.ADVERTIO_CONCURRENCY)


@contextmanager
def request_slot(category):
    from delivery.advertio_client import AdvertioError

    with _SLOTS:
        conn = get_connection()
        try:
            # Standalone legacy clients can run without the application DB.
            # The production startup always creates this ledger. Jobs requires it.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='advertio_request_log'"
            ).fetchone():
                now = datetime.now(timezone.utc)
                day = now.date().isoformat()
                conn.execute("BEGIN IMMEDIATE")
                total = conn.execute(
                    "SELECT COUNT(*) FROM advertio_request_log WHERE attempted_at>=?",
                    (day,),
                ).fetchone()[0]
                jobs = conn.execute(
                    "SELECT COUNT(*) FROM advertio_request_log WHERE attempted_at>=? AND category='jobs'",
                    (day,),
                ).fetchone()[0]
                if total >= config.ADVERTIO_DAILY_REQUEST_LIMIT or (
                    category == "jobs" and jobs >= config.ADVERTIO_JOBS_DAILY_LIMIT
                ):
                    midnight = datetime.combine(
                        now.date() + timedelta(days=1),
                        datetime.min.time(),
                        timezone.utc,
                    )
                    raise AdvertioError(
                        "local_daily_budget",
                        status=429,
                        retryable=True,
                        retry_after=(midnight - now).total_seconds(),
                    )
                conn.execute(
                    "INSERT INTO advertio_request_log(category,attempted_at) VALUES(?,?)",
                    (category, now.isoformat()),
                )
                conn.commit()
            elif category == "jobs":
                raise AdvertioError("jobs_schema_not_initialized")
        finally:
            conn.close()
        yield
