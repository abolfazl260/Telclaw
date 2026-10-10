"""Canonical, read-only pipeline health metrics shared across all UIs.

Use *operation timestamps*, never the Telegram source message's date, for
last-activity checks. Counts represent unique messages, not queued stages.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import config

# A configurable health window is deliberately not used to change scheduler
# behavior; this is a reporting-only threshold.
ACTIVITY_WINDOW = timedelta(hours=24)

PROCESSING_READY = "collection_status='collected' AND processing_status='pending'"
CLASSIFICATION_READY = (
    "processing_status='processed' AND NULLIF(TRIM(ai_category),'') IS NULL "
    "AND (classification_status='pending' OR "
    "(classification_status='failed' AND classification_attempts < ?))"
)
AI_READY = (
    "processing_status='processed' AND classification_status='processed' "
    "AND classification_category IN ('housinglist','transferlist','joblist') "
    "AND ai_status='pending'"
)
ADVERTIO_READY = (
    "ai_status='processed' AND ai_category='housinglist' "
    "AND COALESCE(advertio_status,'waiting') IN ('waiting','retry') "
    "AND EXISTS (SELECT 1 FROM housinglist h WHERE h.processed_message_id=m.id)"
)
FAILED = (
    "processing_status='failed' OR classification_status='failed' "
    "OR ai_status='failed' OR "
    "(ai_category='housinglist' AND advertio_status IN ('failed','rejected'))"
)


def _count(conn, predicate, params=()):
    return int(conn.execute(
        f"SELECT COUNT(*) FROM messages m WHERE {predicate}", params
    ).fetchone()[0] or 0)


def _maximum(conn, column, predicate="1"):
    row = conn.execute(
        f"SELECT MAX({column}) FROM messages WHERE {predicate}"
    ).fetchone()
    return row[0] if row else None


def _last_event(conn, kind):
    # Activity tracking was added after early Back Office test schemas.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_activity'"
    ).fetchone() is None:
        return None, None
    row = conn.execute(
        "SELECT created_at, details FROM system_activity "
        "WHERE kind=? ORDER BY created_at DESC, id DESC LIMIT 1", (kind,)
    ).fetchone()
    if not row:
        return None, None
    try:
        details = json.loads(row["details"]) if row["details"] else None
    except (TypeError, ValueError):
        details = None
    return row["created_at"], details if isinstance(details, dict) else None


def _latest(*values):
    """ISO 8601 UTC stage timestamps sort lexically only after normalization."""
    valid = []
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            valid.append(parsed.astimezone(timezone.utc))
        except (ValueError, TypeError):
            continue
    return max(valid).isoformat() if valid else None


def _recent(value, now):
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return now - dt.astimezone(timezone.utc) <= ACTIVITY_WINDOW


def _stage_status(last, pending, failed, now, *, enabled=True):
    if not enabled:
        return "DISABLED"
    if (pending or failed) and last and not _recent(last, now):
        return "CRITICAL"
    if pending or failed or not _recent(last, now):
        return "WARNING"
    return "HEALTHY"


def collect(conn, *, now=None, quick_check=None):
    """Compute counts and stage health from one SQLite read connection.

    Stage counts may overlap (e.g. a retryable failed classification), but
    backlog and failed-items are COUNT(*) over the OR of predicates, so a
    message is only counted once in each total.
    """
    now = now or datetime.now(timezone.utc)
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "messages" not in existing:
        # Legacy/isolated Back Office installations can initialize publishing
        # rules before the messages schema exists. Health must still render.
        if quick_check is None:
            try:
                quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
            except Exception as exc:
                quick_check = f"error: {exc}"
        zero = {f"{stage}_{suffix}": 0 for stage in
                ("processing", "classification", "ai", "advertio")
                for suffix in ("pending", "failed")}
        return {
            "total": 0, "channels": 0, **zero, "backlog": 0, "failed": 0,
            **{f"last_{stage}": None for stage in
               ("crawl", "processing", "classification", "ai", "advertio")},
            "states": {
                "crawler": "WARNING", "processing": "WARNING",
                "classification": "WARNING" if config.AI_CLASSIFICATION_ENABLED else "DISABLED",
                "ai": "WARNING" if config.AI_EXTRACTION_ENABLED else "DISABLED",
                "advertio": "WARNING" if config.ADVERTIO_INGEST_ENABLED else "DISABLED",
                "database": "HEALTHY" if str(quick_check).lower() == "ok" else "CRITICAL",
            },
            "warning": "No messages table has been initialized.",
            "quick_check": str(quick_check),
        }
    retries = int(config.AI_CLASSIFICATION_MAX_RETRIES)
    processing_pending = _count(conn, PROCESSING_READY)
    classification_pending = _count(conn, CLASSIFICATION_READY, (retries,))
    ai_pending = _count(conn, AI_READY)
    advertio_pending = _count(conn, ADVERTIO_READY)
    processing_failed = _count(conn, "processing_status='failed'")
    classification_failed = _count(conn, "classification_status='failed'")
    ai_failed = _count(conn, "ai_status='failed'")
    advertio_failed = _count(
        conn, "ai_category='housinglist' AND advertio_status IN ('failed','rejected')"
    )
    backlog = _count(
        conn,
        f"({PROCESSING_READY}) OR ({CLASSIFICATION_READY}) OR "
        f"({AI_READY}) OR ({ADVERTIO_READY})",
        (retries,),
    )
    failed = _count(conn, FAILED)
    crawl_at, crawl_details = _last_event(conn, "crawl")
    processing_at, _ = _last_event(conn, "processing")
    classification_at, _ = _last_event(conn, "classification")
    ai_at, _ = _last_event(conn, "ai")
    advertio_at, _ = _last_event(conn, "advertio")
    last_processing = _latest(_maximum(conn, "cleaned_at"), processing_at)
    last_classification = _latest(_maximum(conn, "classification_processed_at"), classification_at)
    last_ai = _latest(_maximum(conn, "ai_processed_at"), ai_at)
    last_advertio = _latest(_maximum(conn, "advertio_processed_at"), advertio_at)
    last_crawl = _latest(crawl_at)

    if quick_check is None:
        try:
            quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        except Exception as exc:
            quick_check = f"error: {exc}"
    database_state = "HEALTHY" if str(quick_check).lower() == "ok" else "CRITICAL"
    crawler_state = _stage_status(last_crawl, 0, 0, now)
    if crawler_state == "HEALTHY" and crawl_details and (
        str(crawl_details.get("status") or "").lower() in {"failed", "stopped", "skipped"}
    ):
        crawler_state = "WARNING"

    classification_enabled = bool(config.AI_CLASSIFICATION_ENABLED)
    extraction_enabled = bool(config.AI_EXTRACTION_ENABLED) or any(
        config.is_ai_extraction_enabled(category)
        for category in ("housinglist", "transferlist", "joblist")
    )
    states = {
        "crawler": crawler_state,
        "processing": _stage_status(last_processing, processing_pending, processing_failed, now),
        "classification": _stage_status(last_classification, classification_pending, classification_failed, now, enabled=classification_enabled),
        "ai": _stage_status(last_ai, ai_pending, ai_failed, now, enabled=extraction_enabled),
        "advertio": _stage_status(last_advertio, advertio_pending, advertio_failed, now, enabled=bool(config.ADVERTIO_INGEST_ENABLED)),
        "database": database_state,
    }
    warning = ""
    if database_state == "CRITICAL":
        warning = f"SQLite quick_check failed: {str(quick_check)[:160]}"
    elif not last_crawl:
        warning = "No actual crawl activity event has been recorded."
    elif not _recent(last_crawl, now):
        warning = "Last recorded crawl is older than 24 hours."

    return {
        "total": _count(conn, "1"),
        "channels": int(conn.execute("SELECT COUNT(DISTINCT channel_username) FROM messages").fetchone()[0] or 0),
        "processing_pending": processing_pending,
        "classification_pending": classification_pending,
        "ai_pending": ai_pending,
        "advertio_pending": advertio_pending,
        "processing_failed": processing_failed,
        "classification_failed": classification_failed,
        "ai_failed": ai_failed,
        "advertio_failed": advertio_failed,
        "backlog": backlog,
        "failed": failed,
        "last_crawl": last_crawl,
        "last_processing": last_processing,
        "last_classification": last_classification,
        "last_ai": last_ai,
        "last_advertio": last_advertio,
        "states": states,
        "warning": warning,
        "quick_check": str(quick_check),
    }


def as_telegram_health(metrics):
    """Compatibility shape for the existing /health command."""
    return {
        **metrics["states"],
        **{key: metrics[key] for key in (
            "last_crawl", "last_processing", "last_classification",
            "last_ai", "last_advertio", "backlog", "failed", "warning",
            "processing_pending", "classification_pending", "ai_pending",
            "advertio_pending", "processing_failed", "classification_failed",
            "ai_failed", "advertio_failed",
        )},
    }
