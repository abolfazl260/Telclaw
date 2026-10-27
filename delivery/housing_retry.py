"""Persistent Housing-only cooldown shared by queued and inline delivery."""
from datetime import datetime, timedelta, timezone
import math
from threading import RLock

from delivery.advertio_client import AdvertioError
from storage.database import get_connection

SEND_LOCK = RLock()


class HousingDeferred(AdvertioError):
    """No API attempt was made because Housing is waiting for its retry time."""


def paused_until():
    conn = get_connection()
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='advertio_housing_cooldown' AND type='table'").fetchone():
            return None
        row = conn.execute('SELECT until FROM advertio_housing_cooldown WHERE id=1').fetchone()
        if row and row[0] > datetime.now(timezone.utc).isoformat():
            return row[0]
        return None
    finally:
        conn.close()


def check():
    until = paused_until()
    if until:
        delay = max(0, (datetime.fromisoformat(until) - datetime.now(timezone.utc)).total_seconds())
        raise HousingDeferred('housing_cooldown', retryable=True, retry_after=delay)


def defer(error):
    """Keep positive upstream waits in full; default 429s to a 60-second pause."""
    if not isinstance(error, AdvertioError) or not (error.retryable or error.status == 429):
        return None
    delay = error.retry_after
    if delay is None or not math.isfinite(delay) or delay <= 0:
        if error.status != 429:
            return None
        delay = 60
    until = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
    conn = get_connection()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS advertio_housing_cooldown (
            id INTEGER PRIMARY KEY CHECK(id=1), until TEXT NOT NULL)''')
        conn.execute('''INSERT INTO advertio_housing_cooldown(id,until) VALUES(1,?)
            ON CONFLICT(id) DO UPDATE SET until=excluded.until
            WHERE excluded.until > advertio_housing_cooldown.until''', (until,))
        conn.commit()
    finally:
        conn.close()
    return paused_until()
