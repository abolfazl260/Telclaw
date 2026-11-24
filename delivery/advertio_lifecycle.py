"""Audit successful ingest identities and propagate confirmed source removal."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import logging
from random import uniform
from uuid import uuid4

import config
from delivery.advertio_client import AdvertioClient, AdvertioError
from storage.database import get_connection

log = logging.getLogger(__name__)
CHECK_INTERVAL = timedelta(hours=6)


def now():
    return datetime.now(timezone.utc)


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS advertio_listing_identity (
            host TEXT NOT NULL, source_name TEXT NOT NULL, external_id TEXT NOT NULL,
            category TEXT NOT NULL, local_id INTEGER, channel_id INTEGER,
            source_channel TEXT, telegram_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
            lead_id TEXT, remote_status TEXT, duplicate_at TEXT, duplicate_first_send INTEGER,
            last_checked TEXT, missing_once INTEGER NOT NULL DEFAULT 0,
            deletion_state TEXT, retry_at TEXT, delete_attempts INTEGER NOT NULL DEFAULT 0,
            claim TEXT, error TEXT,
            PRIMARY KEY(host, source_name, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_advertio_identity_source
            ON advertio_listing_identity(host,channel_id,last_checked);
        CREATE INDEX IF NOT EXISTS idx_advertio_identity_deletion
            ON advertio_listing_identity(host,deletion_state,retry_at);
        CREATE INDEX IF NOT EXISTS idx_advertio_identity_local
            ON advertio_listing_identity(host,category,local_id);
        CREATE INDEX IF NOT EXISTS idx_advertio_identity_duplicates
            ON advertio_listing_identity(duplicate_at);
        CREATE TABLE IF NOT EXISTS advertio_housing_media_key (
            host TEXT NOT NULL, source_name TEXT NOT NULL, external_id TEXT NOT NULL,
            digest TEXT NOT NULL, media_key TEXT NOT NULL, uploaded_at TEXT NOT NULL,
            PRIMARY KEY(host,source_name,external_id,digest)
        );
    """)


def _known_prior_attempt(category, record):
    if category == "housing":
        return record.get("advertio_status") in ("retry", "sent", "already_existed")
    return bool(record.get("attempts"))


def create_listing(client, payload, record, category, *, prepare_payload=None):
    host = getattr(client, "base_url", config.ADVERTIO_BASE_URL)
    mid, channel = record.get("message_id"), record.get("channel_id")
    local_id = record.get("message_row_id") or record.get("processed_message_id") or record.get("id")
    username = str(record.get("channel_username") or "").casefold().lstrip("@")
    with closing(get_connection()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='advertio_listing_identity'").fetchone():
            initialize(conn)
        frozen = conn.execute("""
            SELECT source_name,external_id FROM advertio_listing_identity
            WHERE host=? AND category=? AND local_id=? ORDER BY rowid LIMIT 1
        """, (host, category, local_id)).fetchone() if local_id is not None else None
    if frozen:
        payload = {**payload, "sourceName": frozen["source_name"], "externalId": frozen["external_id"]}
    key = (host, payload["sourceName"], payload["externalId"])
    if prepare_payload is not None:
        prepare_payload(payload)
    first_send = False

    def admitted(conn):
        nonlocal first_send
        # Housing historically assigns one source to multiple Telegram
        # channels. An identical message number can be a different post.
        prior = conn.execute("""
            SELECT local_id,channel_id,source_channel,attempts,deletion_state
            FROM advertio_listing_identity WHERE host=? AND source_name=? AND external_id=?
        """, key).fetchone()
        if prior:
            same_channel = (prior["channel_id"] == channel if prior["channel_id"] and channel
                            else prior["source_channel"] == username)
            if not same_channel or prior["local_id"] not in (None, local_id):
                raise AdvertioError("advertio_source_identity_collision")
            if prior["deletion_state"]:
                raise AdvertioError("advertio_source_post_already_removed")
        if not prior and category == "housing" and mid is not None:
            old_owner = conn.execute("""
                SELECT 1 FROM messages WHERE message_id=? AND channel_username COLLATE NOCASE <> ?
                  AND advertio_status IN ('sent','already_existed') LIMIT 1
            """, (mid, username)).fetchone() if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='messages'"
            ).fetchone() else None
            if old_owner:
                raise AdvertioError("legacy_housing_source_identity_collision")
        first_send = not prior and not _known_prior_attempt(category, record)
        conn.execute("""
            INSERT INTO advertio_listing_identity(
                host,source_name,external_id,category,local_id,channel_id,source_channel,telegram_id,attempts
            ) VALUES(?,?,?,?,?,?,?,?,1)
            ON CONFLICT(host,source_name,external_id)
            DO UPDATE SET attempts=attempts+1
        """, (*key, category, local_id, channel, username, mid))

    if isinstance(client, AdvertioClient):
        response = client.create_lead(payload, on_admit=admitted)
    else:
        with closing(get_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            admitted(conn)
        response = client.create_lead(payload)
    duplicate = bool(response.get("already_existed"))
    stamp = now().isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            UPDATE advertio_listing_identity SET lead_id=?,remote_status=?,
                duplicate_at=COALESCE(duplicate_at,?),
                duplicate_first_send=COALESCE(duplicate_first_send,?)
            WHERE host=? AND source_name=? AND external_id=?
        """, (response["lead_id"], response.get("status"), stamp if duplicate else None,
              int(first_send) if duplicate else None, *key))
    if duplicate:
        log.info("Advertio duplicate category=%s source=%s external=%s local=%s lead=%s first_send=%s at=%s",
                 category, key[1], key[2], local_id, response["lead_id"], first_send, stamp)
    return response


def duplicate_report(limit=100):
    with closing(get_connection()) as conn:
        return [dict(row) for row in conn.execute("""
            SELECT category,source_name,external_id,local_id,lead_id,duplicate_at,
                   duplicate_first_send,remote_status
            FROM advertio_listing_identity WHERE duplicate_at IS NOT NULL
            ORDER BY duplicate_at DESC LIMIT ?
        """, (max(1, min(500, int(limit))),))]


def deletion_candidates(channel_id):
    if not channel_id:
        return []
    cutoff = (now() - CHECK_INTERVAL).isoformat()
    with closing(get_connection()) as conn:
        return [row[0] for row in conn.execute("""
            SELECT DISTINCT telegram_id FROM advertio_listing_identity
            WHERE host=? AND channel_id=? AND telegram_id IS NOT NULL AND lead_id IS NOT NULL
              AND deletion_state IS NULL AND (last_checked IS NULL OR last_checked<=?)
            ORDER BY last_checked,telegram_id LIMIT 20
        """, (config.ADVERTIO_BASE_URL, channel_id, cutoff))]


def record_source_check(channel_id, present, missing):
    if not (present or missing):
        return
    stamp = now().isoformat()
    with closing(get_connection()) as conn, conn:
        conn.executemany("""
            UPDATE advertio_listing_identity
               SET last_checked=?,missing_once=0
             WHERE host=? AND channel_id=? AND telegram_id=? AND deletion_state IS NULL
        """, ((stamp, config.ADVERTIO_BASE_URL, channel_id, mid) for mid in present))
        conn.executemany("""
            UPDATE advertio_listing_identity
               SET last_checked=?,
                   deletion_state=CASE WHEN missing_once=1 THEN 'pending' END,
                   missing_once=1
             WHERE host=? AND channel_id=? AND telegram_id=? AND deletion_state IS NULL
               AND (last_checked IS NULL OR last_checked<=?)
        """, ((stamp, config.ADVERTIO_BASE_URL, channel_id, mid,
               (now() - CHECK_INTERVAL).isoformat()) for mid in missing))


def deliver_deletions(client=None, limit=20, should_stop=None):
    client = client or AdvertioClient(
        config.ADVERTIO_BASE_URL, config.ADVERTIO_INGEST_KEY,
        timeout=config.ADVERTIO_TIMEOUT_SECONDS,
    )
    host = getattr(client, "base_url", config.ADVERTIO_BASE_URL)
    result = {"deleted": 0, "retry": 0, "deferred": 0, "failed": 0}
    for _ in range(max(1, min(50, int(limit)))):
        if should_stop and should_stop():
            break
        token = uuid4().hex
        stamp = now()
        with closing(get_connection()) as conn, conn:
            row = conn.execute("""
                UPDATE advertio_listing_identity
                   SET deletion_state='sending',retry_at=?,claim=?
                 WHERE rowid=(SELECT rowid FROM advertio_listing_identity
                   WHERE host=? AND deletion_state IN ('pending','retry','sending')
                     AND (retry_at IS NULL OR retry_at<=?)
                   ORDER BY COALESCE(retry_at,''),rowid LIMIT 1)
                RETURNING source_name,external_id,delete_attempts
            """, ((stamp + timedelta(seconds=2 * config.ADVERTIO_TIMEOUT_SECONDS + 60)).isoformat(),
                  token, host, stamp.isoformat())).fetchone()
        if not row:
            break
        error = None
        delay = None
        attempted = True
        try:
            status = client.delete_lead(row["source_name"], row["external_id"])
            if status not in (204, 404):
                raise AdvertioError("unexpected_delete_result", retryable=True)
            outcome = "deleted"
        except AdvertioError as exc:
            outcome = "retry" if exc.retryable else "failed"
            error = str(exc)
            attempted = not exc.deferred
            if outcome == "retry":
                delay = max(exc.retry_after or 0, min(21600, 60 * 2 ** min(row["delete_attempts"], 8)
                                                      * uniform(0.9, 1.1)))
        retry_at = (now() + timedelta(seconds=delay)).isoformat() if delay else None
        with closing(get_connection()) as conn, conn:
            conn.execute("""
                UPDATE advertio_listing_identity
                   SET deletion_state=?,retry_at=?,claim=NULL,error=?,delete_attempts=delete_attempts+?
                 WHERE host=? AND source_name=? AND external_id=? AND claim=?
            """, (outcome, retry_at, error, int(attempted), host, row["source_name"],
                  row["external_id"], token))
        result[outcome if attempted else "deferred"] += 1
        if outcome == "retry":
            break
    return result


def housing_media_key(client, path, source, external_id, position):
    # Reuse exact bytes at the same position in this post's album.
    try:
        with Path(path).open("rb") as media:
            content = media.read(8 * 1024 * 1024 + 1)
    except OSError as exc:
        raise AdvertioError("Advertio housing media is unavailable", retryable=True) from exc
    if len(content) > 8 * 1024 * 1024:
        raise AdvertioError("Advertio media file exceeds the 8 MB limit")
    digest = f"{position}:{sha256(content).hexdigest()}"
    host = getattr(client, "base_url", config.ADVERTIO_BASE_URL)
    identity = (host, source, external_id, digest)
    freshness = (now() - timedelta(hours=12)).isoformat()
    with closing(get_connection()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='advertio_housing_media_key'").fetchone():
            initialize(conn)
        row = conn.execute("""
            SELECT media_key FROM advertio_housing_media_key
            WHERE host=? AND source_name=? AND external_id=? AND digest=? AND uploaded_at>=?
        """, (*identity, freshness)).fetchone()
    if row:
        return row["media_key"]
    key = client.upload_media(path, source)
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            INSERT INTO advertio_housing_media_key VALUES(?,?,?,?,?,?)
            ON CONFLICT(host,source_name,external_id,digest) DO UPDATE
            SET media_key=excluded.media_key,uploaded_at=excluded.uploaded_at
        """, (*identity, key, now().isoformat()))
    return key
