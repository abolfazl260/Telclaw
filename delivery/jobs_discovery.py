"""Local-only explanation of Jobs queue admission. Never changes eligibility."""
from datetime import datetime, timedelta, timezone

import config


def discovery_report(conn, *, backfill=False, at=None):
    at = at or datetime.now(timezone.utc)
    days = min(config.ADVERTIO_JOBS_FRESHNESS_DAYS, config.ADVERTIO_JOBS_BACKFILL_DAYS) if backfill else config.ADVERTIO_JOBS_FRESHNESS_DAYS
    row = conn.execute("SELECT value FROM advertio_jobs_meta WHERE key='deployment_watermark'").fetchone()
    watermark = int(row[0]) if row else 0
    rows = conn.execute("""SELECT reason,COUNT(*) FROM (
        SELECT CASE
          WHEN m.ai_status IS NOT 'processed' OR m.ai_category IS NOT 'joblist' THEN 'awaiting_jobs_extraction'
          WHEN j.processed_message_id IS NULL THEN 'missing_extracted_record'
          WHEN m.processing_status IS NOT 'processed' THEN 'processing_incomplete'
          WHEN d.processed_message_id IS NOT NULL THEN 'already_in_delivery_queue'
          WHEN julianday(m.date) IS NULL THEN 'invalid_source_date'
          WHEN julianday(m.date)>julianday(?) THEN 'future_source_date'
          WHEN julianday(m.date)<julianday(?) THEN 'outside_source_date_window'
          WHEN ?=0 AND m.id<=? THEN 'requires_explicit_backfill'
          ELSE 'ready_for_queue'
        END reason
        FROM messages m
        LEFT JOIN joblist j ON j.processed_message_id=m.id
        LEFT JOIN advertio_jobs_deliveries d ON d.processed_message_id=m.id
        WHERE m.ai_category='joblist' OR m.classification_category='joblist'
    ) GROUP BY reason""", (at.isoformat(), (at-timedelta(days=days)).isoformat(), int(backfill), watermark)).fetchall()
    awaiting = conn.execute("""SELECT COALESCE(ai_status,'unset'),COUNT(*) FROM messages
        WHERE (ai_category='joblist' OR classification_category='joblist')
          AND (ai_status IS NOT 'processed' OR ai_category IS NOT 'joblist')
        GROUP BY COALESCE(ai_status,'unset')""").fetchall()
    return dict(awaiting_ai_states={r[0]:r[1] for r in awaiting}, mode='backfill' if backfill else 'new', effective_days=days,
                backfill_enabled=bool(config.ADVERTIO_JOBS_BACKFILL_ENABLED),
                source_date_cutoff=(at-timedelta(days=days)).isoformat(),
                counts={r[0]:r[1] for r in rows})
