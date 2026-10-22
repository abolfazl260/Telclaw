"""Read-only, paginated Back Office report of locally eligible cargo posts.

"Active" here means eligible by the source date, route completeness and
country scope; Telegram deletion state and the platform's live city catalogue
cannot be confirmed from SQLite. No crawl, AI or HTTP requests happen here.
"""
from datetime import datetime, timedelta, timezone
from storage.database import get_connection
from delivery.advertio_cargo import cargo_queue_eligibility_sql
import config

PAGE_SIZE = 25
STATUSES = ("all", "ready", "waiting", "retry", "sent", "already_existed", "rejected", "sending")
TRIPS = ("all", "upcoming", "undated")


def active_cargo_report(*, page=1, status="all", trip="all", search=""):
    try:
        page = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid page") from exc
    if page < 1 or page > 100000:
        raise ValueError("Invalid page")
    if status not in STATUSES or trip not in TRIPS:
        raise ValueError("Invalid cargo filter")
    search = str(search or "").strip()
    if len(search) > 100:
        raise ValueError("Search is too long")
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=config.ADVERTIO_CARGO_BACKFILL_DAYS)).date().isoformat()
    today = now.date().isoformat()
    conn = get_connection()
    try:
        has_messages = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone() is not None
        has_transfer = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transferlist'"
        ).fetchone() is not None
        if not has_messages or not has_transfer:
            return {"rows": [], "total": 0, "page": 1, "pages": 1,
                    "summary": {}, "filters": {"status": status, "trip": trip, "search": search}}
        has_delivery = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='advertio_cargo_deliveries'"
        ).fetchone() is not None
        join = ("LEFT JOIN advertio_cargo_deliveries d ON d.processed_message_id=m.id"
                if has_delivery else "")
        state = "COALESCE(d.status,'waiting')" if has_delivery else "'waiting'"
        next_attempt = "d.next_attempt_at" if has_delivery else "NULL"
        error = "d.error" if has_delivery else "NULL"
        lead = "d.lead_id" if has_delivery else "NULL"
        expr = (
            "m.processing_status='processed' AND m.ai_status='processed' "
            "AND m.ai_category='transferlist' AND (" + cargo_queue_eligibility_sql() + ")"
        )
        base_values = [cutoff, today]
        summary = {"total": 0, "ready": 0, "waiting": 0, "retry": 0,
                   "sent": 0, "already_existed": 0, "rejected": 0, "sending": 0}
        summary_sql = f"""
            SELECT {state} status, COUNT(*) count FROM messages m
            JOIN transferlist t ON t.processed_message_id=m.id
            {join} WHERE {expr} GROUP BY {state}
        """
        for row in conn.execute(summary_sql, base_values):
            name = str(row["status"])
            if name in summary:
                summary[name] += int(row["count"])
                summary["total"] += int(row["count"])
        ready_sql = f"""
            SELECT COUNT(*) FROM messages m
            JOIN transferlist t ON t.processed_message_id=m.id
            {join} WHERE {expr}
            AND {state} IN ('waiting','retry')
            AND ({next_attempt} IS NULL OR {next_attempt}<=?)
        """
        summary["ready"] = int(conn.execute(
            ready_sql, base_values + [now.isoformat()]
        ).fetchone()[0])

        clauses = [expr]
        values = list(base_values)
        if status == "ready":
            clauses.append(f"{state} IN ('waiting','retry')")
            clauses.append(f"({next_attempt} IS NULL OR {next_attempt}<=?)")
            values.append(now.isoformat())
        elif status != "all":
            clauses.append(f"{state}=?")
            values.append(status)
        if trip == "upcoming":
            clauses.append("t.departure_date IS NOT NULL AND TRIM(t.departure_date)<>''")
        elif trip == "undated":
            clauses.append("(t.departure_date IS NULL OR TRIM(t.departure_date)='')")
        if search:
            clauses.append("""(
                m.channel_username LIKE ? OR t.origin_city LIKE ?
                OR t.destination_city LIKE ? OR CAST(m.message_id AS TEXT) LIKE ?
                OR COALESCE(t.title,'') LIKE ?
            )""")
            # Treat % and _ as literal characters to avoid unbounded wildcard input.
            term = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses[-1] = clauses[-1].replace("LIKE ?", "LIKE ? ESCAPE '\\'")
            values.extend([term] * 5)
        where = " AND ".join(clauses)
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM messages m JOIN transferlist t "
            f"ON t.processed_message_id=m.id {join} WHERE {where}", values,
        ).fetchone()[0])
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        sql = f"""
            SELECT m.id AS row_id, m.message_id, m.channel_username,
                   m.message_link, m.date AS posted_at,
                   t.title, t.origin_city, t.origin_country,
                   t.destination_city, t.destination_country,
                   t.departure_date, t.description,
                   {state} AS status, {error} AS error, {lead} AS lead_id,
                   {next_attempt} AS next_attempt_at
            FROM messages m JOIN transferlist t ON t.processed_message_id=m.id
            {join} WHERE {where}
            ORDER BY
              CASE WHEN t.departure_date IS NOT NULL
                         AND TRIM(t.departure_date)<>'' THEN 0 ELSE 1 END,
              CASE WHEN t.departure_date IS NOT NULL
                         AND TRIM(t.departure_date)<>'' THEN date(substr(t.departure_date,1,10)) END ASC,
              CASE WHEN t.departure_date IS NULL
                         OR TRIM(t.departure_date)='' THEN m.date END DESC,
              m.id DESC
            LIMIT ? OFFSET ?
        """
        rows = [dict(row) for row in conn.execute(
            sql, values + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
        )]
        for row in rows:
            row["sendable"] = (
                row["status"] in ("waiting", "retry")
                and (not row["next_attempt_at"] or row["next_attempt_at"] <= now.isoformat())
            )
        return {"rows": rows, "total": total, "page": page,
                "pages": pages, "summary": summary,
                "filters": {"status": status, "trip": trip, "search": search}}
    finally:
        conn.close()
