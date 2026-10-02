"""Read-only operational reporting for the private back office."""

import json
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from storage.database import get_connection

_PROCESS_STARTED_MONOTONIC = time.monotonic()
_CPU_BASE = resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime

TRACKED_TABLES = (
    "messages",
    "transferlist",
    "housinglist",
    "joblist",
    "publishing_targets",
    "publishing_rules",
    "publishing_deliveries",
    "publishing_resends",
    "backoffice_data_edits",
    "system_activity",
)


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _columns(conn, table):
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _count(conn, table, where="1", params=()):
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(f"SELECT COUNT(*) n FROM {table} WHERE {where}", params).fetchone()
    return int(row["n"] or 0)


def _configured_sources():
    sources = {}
    try:
        with open(config.CHANNELS_JSON, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return sources
    if not isinstance(data, dict):
        return sources
    for category, items in data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip().lstrip("@").casefold()
            if not username:
                continue
            sources[username] = {
                "channel_username": username,
                "configured_name": str(item.get("name") or "").strip(),
                "configured_category": str(category),
            }
    return sources


def _database_report(conn):
    quick = conn.execute("PRAGMA quick_check").fetchone()
    quick_check = str(quick[0]) if quick else "unknown"
    row_counts = {table: _count(conn, table) for table in TRACKED_TABLES if _table_exists(conn, table)}
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 0)
    file_size = None
    try:
        file_size = Path(config.DB_NAME).stat().st_size
    except OSError:
        pass
    return {
        "quick_check": quick_check,
        "healthy": quick_check.lower() == "ok",
        "file_size": file_size,
        "allocated_bytes": page_count * page_size,
        "row_counts": row_counts,
    }


def _pipeline_report(conn):
    if not _table_exists(conn, "messages"):
        return {
            "total": 0, "channels": 0, "processing_pending": 0, "processing_failed": 0,
            "classification_pending": 0, "classification_failed": 0, "ai_pending": 0,
            "ai_failed": 0, "advertio_pending": 0, "advertio_failed": 0,
            "last_crawl": None, "last_processing": None, "last_classification": None,
            "last_ai": None, "last_advertio": None,
        }
    columns = _columns(conn, "messages")
    def status_count(column, value, extra=None):
        if column not in columns:
            return 0
        where = f"{column}=?"
        params = [value]
        if extra:
            where += " AND " + extra
        return _count(conn, "messages", where, params)

    total = _count(conn, "messages")
    channels = 0
    if "channel_username" in columns:
        row = conn.execute("SELECT COUNT(DISTINCT channel_username) n FROM messages").fetchone()
        channels = int(row["n"] or 0)

    def max_column(column, where="1", params=()):
        if column not in columns:
            return None
        row = conn.execute(
            f"SELECT MAX({column}) value FROM messages WHERE {where}",
            params,
        ).fetchone()
        return row["value"] if row and row["value"] else None

    advertio_pending = 0
    if {"advertio_status", "ai_status"} <= columns:
        advertio_pending = _count(
            conn, "messages",
            "ai_status='processed' AND COALESCE(advertio_status,'waiting') IN ('waiting','retry')",
        )

    return {
        "total": total,
        "channels": channels,
        "processing_pending": status_count("processing_status", "pending", "collection_status='collected'") if "collection_status" in columns else status_count("processing_status", "pending"),
        "processing_failed": status_count("processing_status", "failed"),
        "classification_pending": status_count("classification_status", "pending"),
        "classification_failed": status_count("classification_status", "failed"),
        "ai_pending": status_count("ai_status", "pending"),
        "ai_failed": status_count("ai_status", "failed"),
        "advertio_pending": advertio_pending,
        "advertio_failed": status_count("advertio_status", "failed"),
        "last_crawl": max_column("date", "collection_status='collected'") if "collection_status" in columns else max_column("date"),
        "last_processing": max_column("cleaned_at"),
        "last_classification": max_column("classification_processed_at"),
        "last_ai": max_column("ai_processed_at"),
        "last_advertio": max_column("advertio_processed_at"),
    }


def _daily_crawl_report(conn, limit=14):
    if not _table_exists(conn, "messages"):
        return []
    columns = _columns(conn, "messages")
    if "date" not in columns:
        return []
    optional = {
        "processed": "SUM(CASE WHEN processing_status='processed' THEN 1 ELSE 0 END)" if "processing_status" in columns else "0",
        "classified": "SUM(CASE WHEN classification_status='processed' THEN 1 ELSE 0 END)" if "classification_status" in columns else "0",
        "ai_processed": "SUM(CASE WHEN ai_status='processed' THEN 1 ELSE 0 END)" if "ai_status" in columns else "0",
        "advertio_sent": "SUM(CASE WHEN advertio_status='sent' THEN 1 ELSE 0 END)" if "advertio_status" in columns else "0",
    }
    failure_parts = []
    for column in ("processing_status", "classification_status", "ai_status", "advertio_status"):
        if column in columns:
            failure_parts.append(f"{column}='failed'")
    failures = (
        "SUM(CASE WHEN " + " OR ".join(failure_parts) + " THEN 1 ELSE 0 END)"
        if failure_parts else "0"
    )
    sql = f"""SELECT substr(date,1,10) day, COUNT(*) crawled,
        {optional['processed']} processed,
        {optional['classified']} classified,
        {optional['ai_processed']} ai_processed,
        {optional['advertio_sent']} advertio_sent,
        {failures} failed
        FROM messages
        WHERE date IS NOT NULL AND TRIM(date)<>''
        GROUP BY substr(date,1,10)
        ORDER BY day DESC LIMIT ?"""
    return [dict(row) for row in conn.execute(sql, (min(max(int(limit), 1), 60),)).fetchall()]


def _daily_stage_activity_report(conn, limit=14):
    """Group successful pipeline work by the timestamp of the stage itself."""
    if not _table_exists(conn, "messages"):
        return []
    columns = _columns(conn, "messages")
    selects = []

    if {"cleaned_at", "processing_status"} <= columns:
        selects.append(
            """SELECT substr(cleaned_at,1,10) day, 1 processed, 0 classified, 0 ai_processed, 0 advertio_sent
                 FROM messages
                WHERE processing_status='processed'
                  AND cleaned_at IS NOT NULL AND TRIM(cleaned_at)<>''"""
        )
    if {"classification_processed_at", "classification_status"} <= columns:
        selects.append(
            """SELECT substr(classification_processed_at,1,10) day, 0 processed, 1 classified, 0 ai_processed, 0 advertio_sent
                 FROM messages
                WHERE classification_status='processed'
                  AND classification_processed_at IS NOT NULL
                  AND TRIM(classification_processed_at)<>''"""
        )
    if {"ai_processed_at", "ai_status"} <= columns:
        selects.append(
            """SELECT substr(ai_processed_at,1,10) day, 0 processed, 0 classified, 1 ai_processed, 0 advertio_sent
                 FROM messages
                WHERE ai_status='processed'
                  AND ai_processed_at IS NOT NULL AND TRIM(ai_processed_at)<>''"""
        )
    if {"advertio_processed_at", "advertio_status"} <= columns:
        selects.append(
            """SELECT substr(advertio_processed_at,1,10) day, 0 processed, 0 classified, 0 ai_processed, 1 advertio_sent
                 FROM messages
                WHERE advertio_status='sent'
                  AND advertio_processed_at IS NOT NULL
                  AND TRIM(advertio_processed_at)<>''"""
        )

    if not selects:
        return []

    sql = f"""SELECT day,
        SUM(processed) processed,
        SUM(classified) classified,
        SUM(ai_processed) ai_processed,
        SUM(advertio_sent) advertio_sent
        FROM ({' UNION ALL '.join(selects)})
        WHERE day IS NOT NULL AND TRIM(day)<>''
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?"""
    return [
        dict(row)
        for row in conn.execute(sql, (min(max(int(limit), 1), 60),)).fetchall()
    ]


def _channel_report(conn):
    configured = _configured_sources()
    db_rows = []
    if _table_exists(conn, "messages"):
        columns = _columns(conn, "messages")
        if "channel_username" in columns:
            name_expr = "MAX(channel_name)" if "channel_name" in columns else "NULL"
            first_expr = "MIN(date)" if "date" in columns else "NULL"
            last_expr = "MAX(date)" if "date" in columns else "NULL"
            latest_id = "MAX(message_id)" if "message_id" in columns else "NULL"
            failure_parts = [
                f"{column}='failed'"
                for column in ("processing_status", "classification_status", "ai_status", "advertio_status")
                if column in columns
            ]
            failed_expr = (
                "SUM(CASE WHEN " + " OR ".join(failure_parts) + " THEN 1 ELSE 0 END)"
                if failure_parts else "0"
            )
            db_rows = [dict(row) for row in conn.execute(f"""SELECT
                channel_username, {name_expr} channel_name, COUNT(*) messages,
                {first_expr} first_message, {last_expr} last_message,
                {latest_id} latest_message_id, {failed_expr} failed
                FROM messages
                GROUP BY channel_username
                ORDER BY last_message DESC, messages DESC""").fetchall()]

    seen = set()
    rows = []
    for row in db_rows:
        key = str(row.get("channel_username") or "").strip().lstrip("@").casefold()
        meta = configured.get(key, {})
        row["configured"] = key in configured
        row["configured_name"] = meta.get("configured_name") or ""
        row["configured_category"] = meta.get("configured_category") or ""
        row["crawl_state"] = "crawled"
        rows.append(row)
        seen.add(key)
    for key, meta in configured.items():
        if key in seen:
            continue
        rows.append({
            "channel_username": key,
            "channel_name": "",
            "messages": 0,
            "first_message": None,
            "last_message": None,
            "latest_message_id": None,
            "failed": 0,
            "configured": True,
            "configured_name": meta.get("configured_name") or "",
            "configured_category": meta.get("configured_category") or "",
            "crawl_state": "not crawled",
        })
    return rows


def _recent_activity(conn, limit=60):
    if not _table_exists(conn, "system_activity"):
        return []
    rows = [dict(row) for row in conn.execute("""SELECT id,kind,level,source,message,details,created_at
        FROM system_activity ORDER BY id DESC LIMIT ?""",
        (min(max(int(limit), 1), 200),)).fetchall()]
    for row in rows:
        raw = row.get("details")
        if raw:
            try:
                row["details_data"] = json.loads(raw)
            except (ValueError, TypeError):
                row["details_data"] = raw
        else:
            row["details_data"] = None
    return rows


def _recent_edits(conn, limit=20):
    if not _table_exists(conn, "backoffice_data_edits"):
        return []
    return [dict(row) for row in conn.execute("""SELECT admin_id,table_name,row_id,column_name,
        old_value,new_value,edited_at FROM backoffice_data_edits
        ORDER BY id DESC LIMIT ?""", (min(max(int(limit), 1), 100),)).fetchall()]


def _publishing_report(conn):
    result = {"targets": 0, "rules": 0, "deliveries": {}, "resends": {}}
    result["targets"] = _count(conn, "publishing_targets")
    result["rules"] = _count(conn, "publishing_rules")
    for table, key in (("publishing_deliveries", "deliveries"), ("publishing_resends", "resends")):
        if not _table_exists(conn, table) or "status" not in _columns(conn, table):
            continue
        result[key] = {
            str(row["status"]): int(row["n"] or 0)
            for row in conn.execute(
                f"SELECT status,COUNT(*) n FROM {table} GROUP BY status ORDER BY status"
            ).fetchall()
        }
    return result


def _system_report(conn):
    """Return live process, database, queue, error-rate and uptime metrics."""
    now = time.monotonic()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_total = usage.ru_utime + usage.ru_stime
    elapsed = max(now - _PROCESS_STARTED_MONOTONIC, 0.001)
    cpu_percent = max(0.0, min(100.0, ((cpu_total - _CPU_BASE) / elapsed) * 100.0))
    rss = int(usage.ru_maxrss or 0)
    if os.name == "posix" and os.uname().sysname.lower() == "darwin":
        ram_bytes = rss
    else:
        ram_bytes = rss * 1024

    columns = _columns(conn, "messages") if _table_exists(conn, "messages") else set()
    queue_parts = []
    for column, value in (
        ("processing_status", "pending"),
        ("classification_status", "pending"),
        ("ai_status", "pending"),
        ("advertio_status", "waiting"),
    ):
        if column in columns:
            queue_parts.append(_count(conn, "messages", f"{column}=?", (value,)))
    queue_size = sum(queue_parts)

    today = datetime.now(timezone.utc).date().isoformat()
    daily_ads = 0
    daily_total = 0
    daily_failed = 0
    if "date" in columns:
        daily_total = _count(conn, "messages", "substr(date,1,10)=?", (today,))
        category_filter = "ai_category IN ('housinglist','transferlist','joblist')" if "ai_category" in columns else "0"
        daily_ads = _count(conn, "messages", f"substr(date,1,10)=? AND {category_filter}", (today,))
        failure_parts = [f"{column}='failed'" for column in ("processing_status", "classification_status", "ai_status", "advertio_status") if column in columns]
        if failure_parts:
            daily_failed = _count(conn, "messages", f"substr(date,1,10)=? AND ({' OR '.join(failure_parts)})", (today,))
    error_rate = (daily_failed / daily_total * 100.0) if daily_total else 0.0

    return {
        "daily_ads": daily_ads,
        "cpu_percent": round(cpu_percent, 1),
        "ram_bytes": ram_bytes,
        "db_size_bytes": _database_report(conn).get("file_size"),
        "queue_size": queue_size,
        "error_rate": round(error_rate, 1),
        "uptime_seconds": int(elapsed),
        "daily_total_messages": daily_total,
        "daily_failed": daily_failed,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


def _overview_report(conn):
    """Return compact business metrics for the back-office overview."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    new_messages = 0
    duplicates = 0

    if _table_exists(conn, "system_activity"):
        rows = conn.execute(
            """SELECT kind, details
               FROM system_activity
               WHERE created_at >= ? AND kind IN ('crawl', 'processing')""",
            (today,),
        ).fetchall()
        for row in rows:
            raw = row["details"]
            if not raw:
                continue
            try:
                details = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(details, dict):
                continue
            if row["kind"] == "crawl":
                for key in ("saved", "new_messages", "collected"):
                    if key in details:
                        try:
                            new_messages += max(0, int(details[key] or 0))
                        except (TypeError, ValueError):
                            pass
                        break
            elif row["kind"] == "processing":
                try:
                    duplicates += max(0, int(details.get("duplicates_removed", 0) or 0))
                except (TypeError, ValueError):
                    pass

    ready_ads = 0
    if _table_exists(conn, "messages"):
        columns = _columns(conn, "messages")
        if {"ai_status", "ai_category"} <= columns:
            where = """ai_status='processed'
                       AND ai_category IN ('housinglist','transferlist','joblist')"""
            if _table_exists(conn, "publishing_deliveries"):
                where += """ AND NOT EXISTS (
                    SELECT 1 FROM publishing_deliveries d
                    WHERE d.message_id = messages.id AND d.status='sent'
                )"""
            ready_ads = _count(conn, "messages", where)

    providers = []
    for name in getattr(config, "AI_PROVIDERS", ()):
        provider = str(name).strip().lower()
        if provider == "groq":
            credential_count = len(getattr(config, "GROQ_PROVIDERS", ()) or ())
        elif provider == "cloudflare":
            credential_count = len(getattr(config, "CLOUDFLARE_PROVIDERS", ()) or ())
        else:
            credential_count = 0
        providers.append({
            "name": provider,
            "priority": len(providers) + 1,
            "credential_count": credential_count,
            "status": "READY" if credential_count else "NOT CONFIGURED",
        })

    return {
        "new_messages": new_messages,
        "duplicates": duplicates,
        "ready_ads": ready_ads,
        "providers": providers,
        "provider_enabled": bool(getattr(config, "AI_EXTRACTION_ENABLED", False)),
        "date": today,
    }


def snapshot():
    """Build one consistent read-only health snapshot from SQLite and configuration."""
    conn = get_connection()
    try:
        subscribers = _count(conn, "telegram_monitor_subscribers", "enabled=1")
        return {
            "database": _database_report(conn),
            "pipeline": _pipeline_report(conn),
            "daily": _daily_crawl_report(conn),
            "stage_daily": _daily_stage_activity_report(conn),
            "channels": _channel_report(conn),
            "activity": _recent_activity(conn),
            "edits": _recent_edits(conn),
            "publishing": _publishing_report(conn),
            "overview": _overview_report(conn),
            "system": _system_report(conn),
            "bot": {
                "monitor_configured": bool(config.TELEGRAM_MONITOR_ENABLED),
                "token_configured": bool(config.TELEGRAM_BOT_TOKEN),
                "active_subscribers": subscribers,
                "backoffice_enabled": bool(config.BACKOFFICE_ENABLED),
                "classification_enabled": bool(config.AI_CLASSIFICATION_ENABLED),
                "extraction_enabled": bool(config.AI_EXTRACTION_ENABLED),
                "advertio_enabled": bool(config.ADVERTIO_INGEST_ENABLED),
            },
        }
    finally:
        conn.close()
