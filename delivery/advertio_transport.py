"""Shared SQLite reservations, admission pacing and cooldown for Advertio HTTP."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import logging
import math
import time
from uuid import uuid4

import config
from storage.database import get_connection

log = logging.getLogger(__name__)
PACE_SECONDS = 0.15


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS advertio_request_log (
            id INTEGER PRIMARY KEY, category TEXT NOT NULL, attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_advertio_request_day
            ON advertio_request_log(attempted_at,category);
        CREATE TABLE IF NOT EXISTS advertio_http_slot (
            token TEXT PRIMARY KEY, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS advertio_http_state (
            id INTEGER PRIMARY KEY CHECK(id=1), until TEXT NOT NULL, reason TEXT NOT NULL
        );
    """)


def now():
    return datetime.now(timezone.utc)


def _midnight(stamp):
    return datetime.combine(stamp.date() + timedelta(days=1), datetime.min.time(), timezone.utc)


def _daily_counts(conn, stamp):
    return dict(conn.execute("""
        SELECT category,COUNT(*) FROM advertio_request_log
         WHERE attempted_at>=? GROUP BY category
    """, (stamp.date().isoformat(),)))


def _deferred(reason, delay):
    from delivery.advertio_client import AdvertioError

    return AdvertioError(reason, status=429, retryable=True, retry_after=delay, deferred=True)


def _reserve(conn, category, token, timeout, on_admit):
    stamp = now()
    state = conn.execute("SELECT until,reason FROM advertio_http_state WHERE id=1").fetchone()
    if state and (remaining := (datetime.fromisoformat(state["until"]) - stamp).total_seconds()) > 0:
        raise _deferred(state["reason"], remaining)
    counts = _daily_counts(conn, stamp)
    total = sum(counts.values())
    limit = config.ADVERTIO_DAILY_REQUEST_LIMIT
    # Keep a guaranteed share for each journey and essential reference
    # and deletion work. Any category may also use the shared remainder.
    counts["housing"] = counts.get("housing", 0) + counts.get("legacy", 0)
    bucket = "housing" if category == "legacy" else category
    share = limit // 20
    protected = sum(
        max(0, share - counts.get(other, 0))
        for other in ("housing", "cargo", "jobs", "catalog", "deletion") if other != bucket
    )
    if total >= limit - protected or (
        bucket == "jobs" and counts.get("jobs", 0) >= config.ADVERTIO_JOBS_DAILY_LIMIT
    ):
        raise _deferred("local_daily_budget", (_midnight(stamp) - stamp).total_seconds())
    conn.execute("DELETE FROM advertio_http_slot WHERE expires_at<=?", (stamp.isoformat(),))
    if conn.execute("SELECT COUNT(*) FROM advertio_http_slot").fetchone()[0] >= config.ADVERTIO_CONCURRENCY:
        raise _deferred("local_concurrency_busy", 2)
    last = conn.execute("SELECT MAX(attempted_at) FROM advertio_request_log").fetchone()[0]
    if last:
        pace = PACE_SECONDS - (stamp - datetime.fromisoformat(last)).total_seconds()
        if 0 < pace <= PACE_SECONDS:
            return pace
    if on_admit is not None:
        on_admit(conn)
    conn.execute("INSERT INTO advertio_http_slot VALUES (?,?)",
                 (token, (stamp + timedelta(seconds=2 * timeout + 60)).isoformat()))
    conn.execute("INSERT INTO advertio_request_log(category,attempted_at) VALUES (?,?)",
                 (category, stamp.isoformat()))
    return 0


@contextmanager
def request_slot(category, timeout=60, *, on_admit=None):
    token = uuid4().hex
    with closing(get_connection()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='advertio_http_state'").fetchone():
            initialize(conn)
        for _ in range(3):
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                delay = _reserve(conn, category, token, timeout, on_admit)
            if not delay:
                break
            time.sleep(delay)
        else:
            raise _deferred("local_request_pacing", 1)
        try:
            yield
        finally:
            with conn:
                conn.execute("DELETE FROM advertio_http_slot WHERE token=?", (token,))


def record_cooldown(status, retry_after=None):
    # Client validation errors are permanent for the individual payload.
    delay = {401: 3600, 404: 3600, 429: 60}.get(status)
    if delay is None:
        return
    if retry_after is not None and math.isfinite(retry_after):
        delay = max(delay, retry_after)
    until = (now() + timedelta(seconds=delay)).isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            INSERT INTO advertio_http_state(id,until,reason) VALUES(1,?,?)
            ON CONFLICT(id) DO UPDATE SET until=excluded.until,reason=excluded.reason
            WHERE excluded.until>advertio_http_state.until
        """, (until, f"upstream_http_{status}"))
    log.warning("Advertio shared cooldown for HTTP %s until %s", status, until)


def metadata(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='advertio_http_state'").fetchone():
        return {}
    stamp = now()
    counts = _daily_counts(conn, stamp)
    state = conn.execute(
        "SELECT until,reason FROM advertio_http_state WHERE id=1 AND until>?",
        (stamp.isoformat(),),
    ).fetchone()
    return {
        "by_category": counts,
        "remaining": max(0, config.ADVERTIO_DAILY_REQUEST_LIMIT - sum(counts.values())),
        "resets_at": _midnight(stamp).isoformat(),
        "cooldown": dict(state) if state else None,
    }
