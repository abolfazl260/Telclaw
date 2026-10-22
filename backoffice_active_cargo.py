"""Read-only, paginated Back Office report of locally eligible cargo posts.

"Active" here means eligible by the source date, route completeness and
country scope; Telegram deletion state and the platform's live city catalogue
cannot be confirmed from SQLite. No crawl, AI or HTTP requests happen here.
"""
from datetime import datetime, timedelta, timezone
from storage.database import get_connection
from delivery.advertio_cargo import COUNTRIES, cargo_queue_eligibility_sql
from monitoring.transfer_live import active_transferlive_message_ids
import config

PAGE_SIZE = 25
STATUSES = ("all", "ready", "waiting", "retry", "sent", "already_existed", "rejected", "sending")
TRIPS = ("all", "upcoming", "undated")
VIEWS = ("advertio", "telegram")


def active_cargo_report(*, page=1, status="all", trip="all", search="", view="advertio"):
    try:
        page = int(page)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid page") from exc
    if page < 1 or page > 100000:
        raise ValueError("Invalid page")
    if status not in STATUSES or trip not in TRIPS or view not in VIEWS:
        raise ValueError("Invalid cargo filter")
    search = str(search or "").strip()
    if len(search) > 100:
        raise ValueError("Search is too long")
    # Calculate parity directly from the SAME deduplicated /transferlive
    # cohort, not a second approximation of sender/route uniqueness.
    live_ids = active_transferlive_message_ids()
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
                    "summary": {"total": 0, "ready": 0, "transferlive": len(live_ids)},
                    "filters": {"status": status, "trip": trip, "search": search, "view": view}}
        has_delivery = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='advertio_cargo_deliveries'"
        ).fetchone() is not None
        join = ("LEFT JOIN advertio_cargo_deliveries d ON d.processed_message_id=m.id"
                if has_delivery else "")
        state = "COALESCE(d.status,'waiting')" if has_delivery else "'waiting'"
        next_attempt = "d.next_attempt_at" if has_delivery else "NULL"
        error = "d.error" if has_delivery else "NULL"
        lead = "d.lead_id" if has_delivery else "NULL"
        if view == "telegram":
            # SQLite TEMP table is connection-private, not a mutation of
            # the actual user database. Avoid a massive SQL IN parameter
            # list, which fails on older SQLite builds above 999 IDs.
            conn.execute("CREATE TEMP TABLE live_transfer_ids(message_row_id INTEGER PRIMARY KEY)")
            conn.executemany(
                "INSERT INTO live_transfer_ids(message_row_id) VALUES(?)",
                ((value,) for value in live_ids),
            )
        # Active is a date/freshness rule, NOT an assertion that a
        # Telegram post still exists. Unsupported routes remain visible in
        # the Advertio candidate view, but are not eligible for API sending.
        active_sql = """(
            ((t.departure_date IS NULL OR TRIM(t.departure_date)='')
              AND substr(m.date,1,10)>=?)
            OR (t.departure_date IS NOT NULL AND TRIM(t.departure_date)<>''
              AND date(substr(t.departure_date,1,10))>=date(?))
        )"""
        if view == "telegram":
            # This view is EXACTLY the same ID set used by the bot, including
            # its sender/route/date deduplication and optional city fields.
            expr = "m.id IN (SELECT message_row_id FROM live_transfer_ids)"
            base_values = []
        else:
            expr = (
                "m.processing_status='processed' AND m.ai_status='processed' "
                "AND m.ai_category='transferlist' AND " + active_sql
            )
            base_values = [cutoff, today]
        summary = {"total": 0, "ready": 0, "transferlive": len(live_ids),
                   "waiting": 0, "retry": 0,
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
            AND ({cargo_queue_eligibility_sql()})
            AND {state} IN ('waiting','retry')
            AND ({next_attempt} IS NULL OR {next_attempt}<=?)
        """
        summary["ready"] = int(conn.execute(
            ready_sql, base_values + [cutoff, today, now.isoformat()]
        ).fetchone()[0])

        clauses = [expr]
        values = list(base_values)
        if status == "ready":
            clauses.append("(" + cargo_queue_eligibility_sql() + ")")
            values.extend([cutoff, today])
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
            country_ok = (
                str(row.get("origin_country") or "").strip().upper() in COUNTRIES
                and str(row.get("destination_country") or "").strip().upper() in COUNTRIES
            )
            route_ok = bool(
                str(row.get("origin_city") or "").strip()
                and str(row.get("destination_city") or "").strip()
            )
            if not country_ok:
                row["blocked_reason"] = "Country outside Advertio catalog"
            elif not route_ok:
                row["blocked_reason"] = "Incomplete origin/destination"
            else:
                row["blocked_reason"] = None
            row["sendable"] = (
                country_ok and route_ok
                and row["status"] in ("waiting", "retry")
                and (not row["next_attempt_at"] or row["next_attempt_at"] <= now.isoformat())
            )
        return {"rows": rows, "total": total, "page": page,
                "pages": pages, "summary": summary,
                "filters": {"status": status, "trip": trip, "search": search, "view": view}}
    finally:
        conn.close()
