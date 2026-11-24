"""Publication outcomes observed at ingest; no live API calls."""
import config

OUTCOMES = ("active", "pending_review", "publication_unknown", "publication_other")


def outcome(status):
    if not isinstance(status, str) or not status.strip():
        return "publication_unknown"
    value = status.strip().casefold()
    if value == "active":
        return "active"
    if value == "pendingreview":
        return "pending_review"
    return "publication_other"


def _has_identity(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='advertio_listing_identity'"
    ).fetchone() is not None


def _status_sql():
    # Match the exact accepted lead and local record on the configured host.
    # The scalar query avoids inflating counts for historical source identities.
    return """(SELECT i.remote_status FROM advertio_listing_identity i
        WHERE i.host=? AND i.category='cargo'
          AND i.local_id=d.processed_message_id AND i.lead_id=d.lead_id
        ORDER BY i.rowid DESC LIMIT 1)"""


def annotate(conn, rows):
    accepted = [row for row in rows if row["status"] in ("sent", "already_existed")]
    if not accepted:
        return
    if not _has_identity(conn):
        for row in accepted:
            row["remote_status"] = None
        return
    ids = tuple(row["row_id"] for row in accepted)
    found = dict(conn.execute(f"""SELECT d.processed_message_id, {_status_sql()}
        FROM advertio_cargo_deliveries d
        WHERE d.processed_message_id IN ({','.join('?' for _ in ids)})""",
        (config.ADVERTIO_BASE_URL, *ids)).fetchall())
    for row in accepted:
        row["remote_status"] = found.get(row["row_id"])


def totals(conn):
    """Count all locally accepted new cargo, not duplicates or current live inventory."""
    counts = dict.fromkeys(OUTCOMES, 0)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='advertio_cargo_deliveries'"
    ).fetchone():
        return counts
    if not _has_identity(conn):
        counts["publication_unknown"] = conn.execute(
            "SELECT COUNT(*) FROM advertio_cargo_deliveries WHERE status='sent'"
        ).fetchone()[0]
        return counts
    for row in conn.execute(f"""SELECT {_status_sql()} remote_status, COUNT(*) n
        FROM advertio_cargo_deliveries d WHERE d.status='sent' GROUP BY 1""",
        (config.ADVERTIO_BASE_URL,)):
        counts[outcome(row[0])] += row[1]
    return counts
