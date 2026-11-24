"""Bounded, explicitly reviewed historical cargo recovery and route diagnostics."""

from collections import Counter
from contextlib import closing
from datetime import timedelta

import config
from delivery import advertio_cargo_catalog as catalog
from storage.database import get_connection

LEGACY_LOCATION_ERRORS = (
    "d.error LIKE 'Cargo route country outside Advertio catalog:%' "
    "OR d.error LIKE 'City is not in Advertio catalog:%'"
)


def _rows(conn, *, where, params=(), limit=100, after=0):
    stamp = catalog.now()
    cutoff = (stamp.date() - timedelta(days=config.ADVERTIO_CARGO_BACKFILL_DAYS)).isoformat()
    return [dict(row) for row in conn.execute(f"""
        SELECT m.id AS local_id,m.message_id,m.message_link,m.channel_username,m.channel_id,m.date AS posted_at,
               {catalog.ROUTE_EVIDENCE_SQL} AS evidence,
               t.origin_country,t.origin_city,t.destination_country,t.destination_city,
               COALESCE(d.status,'waiting') AS status,d.error
          FROM messages m JOIN transferlist t ON t.processed_message_id=m.id
          LEFT JOIN advertio_cargo_deliveries d ON d.processed_message_id=m.id
         WHERE m.id>? AND m.processing_status='processed' AND m.ai_status='processed'
           AND m.ai_category='transferlist' AND {catalog.TRIP_WINDOW_SQL}
           AND ({where})
         ORDER BY m.id LIMIT ?
    """, (after, cutoff, stamp.date().isoformat(), *params, limit))]


def review_history(*, after=0, limit=50, apply_ids=()):
    """Use the installed snapshot only. An apply never makes an HTTP request."""
    apply_ids = tuple(dict.fromkeys(int(value) for value in apply_ids))
    limit = max(1, min(100, int(limit)))
    if len(apply_ids) > limit or any(value <= 0 for value in apply_ids):
        raise ValueError("Select up to the report limit of positive record IDs")
    with closing(get_connection()) as conn, conn:
        if apply_ids:
            conn.execute("BEGIN IMMEDIATE")
        snapshot = catalog.load(conn)
        if snapshot is None or snapshot.stale:
            raise ValueError("A fresh, successful Advertio catalog snapshot is required")
        catalog.register_sql(conn, snapshot)
        where = f"""
          (({catalog.UNREVIEWED_HISTORY_SQL}) AND d.processed_message_id IS NULL
           OR (d.status='rejected' AND d.lead_id IS NULL AND ({LEGACY_LOCATION_ERRORS})))
          AND advertio_cargo_route_ok(t.origin_country,t.origin_city,t.destination_country,
              t.destination_city,{catalog.ROUTE_EVIDENCE_SQL})=1
        """
        if apply_ids:
            where += " AND m.id IN (" + ",".join("?" for _ in apply_ids) + ")"
        rows = _rows(conn, where=where, params=apply_ids, limit=limit, after=0 if apply_ids else after)
        for row in rows:
            row.pop("evidence")
            row["reason"] = "legacy_location_rejection" if row["status"] == "rejected" else "legacy_country_filter"
        counts = Counter((row["reason"], row["origin_country"], row["destination_country"]) for row in rows)
        if apply_ids:
            stamp = catalog.now().isoformat()
            for row in rows:
                conn.execute("""
                    INSERT OR IGNORE INTO advertio_cargo_history_review(local_id,reviewed_at,reason)
                    VALUES (?,?,?)
                """, (row["local_id"], stamp, row["reason"]))
                if row["status"] == "rejected":
                    conn.execute("""
                        UPDATE advertio_cargo_deliveries SET status='waiting',next_attempt_at=NULL,updated_at=?
                        WHERE processed_message_id=? AND status='rejected' AND lead_id IS NULL
                    """, (stamp, row["local_id"]))
        return {
            "rows": rows,
            "counts": [{"reason": reason, "origin_country": origin, "destination_country": dest, "count": n}
                       for (reason, origin, dest), n in counts.items()],
            "next_after": rows[-1]["local_id"] if len(rows) == limit else None,
            "applied": [row["local_id"] for row in rows] if apply_ids else [],
            "skipped": sorted(set(apply_ids) - {row["local_id"] for row in rows}),
        }


def route_diagnostics(*, after=0, limit=100):
    """Group current unsupported routes by side and spelling for catalog owners."""
    limit = max(1, min(500, int(limit)))
    with closing(get_connection()) as conn:
        snapshot = catalog.load(conn)
        if snapshot is None or snapshot.stale:
            raise ValueError("A fresh, successful Advertio catalog snapshot is required")
        catalog.register_sql(conn, snapshot)
        rows = _rows(conn, where=f"""
            COALESCE(d.status,'waiting') IN ('waiting','retry','rejected')
            AND advertio_cargo_route_ok(t.origin_country,t.origin_city,t.destination_country,
                t.destination_city,{catalog.ROUTE_EVIDENCE_SQL})=0
        """, limit=limit, after=after)
        groups = {}
        for row in rows:
            for side in ("origin", "destination"):
                city = row[side + "_city"]
                _, reason, country = snapshot.resolve(side, row[side + "_country"], city, row["evidence"])
                if reason not in ("unsupported_city", "unsupported_country", "ambiguous_city"):
                    continue
                key = (side, reason, country, catalog.normalize(city))
                item = groups.setdefault(key, {
                    "direction": side, "reason": reason, "country": country or row[side + "_country"],
                    "city": city, "count": 0, "samples": [], "latest_seen": None,
                })
                item["count"] += 1
                item["latest_seen"] = max(item["latest_seen"] or "", row["posted_at"])
                if len(item["samples"]) < 3:
                    item["samples"].append({"message_id": row["message_id"], "link": row["message_link"]})
        return {"routes": list(groups.values()),
                "next_after": rows[-1]["local_id"] if len(rows) == limit else None}
