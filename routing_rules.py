"""SQLite-backed publishing rules and per-destination delivery state."""
from datetime import datetime, timedelta, timezone
import math
import re

from storage.database import CATEGORY_TABLES, get_connection

CATEGORIES = ("transferlist", "housinglist", "joblist")
SCOPES = ("either", "origin", "destination")
FILTER_FIELDS = {category: tuple(fields) for category, fields in CATEGORY_TABLES.items()
                 if category in CATEGORIES}


def initialize():
    conn = get_connection()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS publishing_targets (
            id INTEGER PRIMARY KEY, label TEXT NOT NULL,
            chat_id TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT NOT NULL DEFAULT '', connection_status TEXT NOT NULL DEFAULT 'unknown',
            connection_detail TEXT NOT NULL DEFAULT '', checked_at TEXT,
            rate_limited_until TEXT
        );
        CREATE TABLE IF NOT EXISTS publishing_rules (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            category TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
            country_scope TEXT NOT NULL DEFAULT 'either',
            source_channel TEXT NOT NULL DEFAULT '',
            origin_city TEXT NOT NULL DEFAULT '',
            destination_city TEXT NOT NULL DEFAULT '',
            min_price REAL, max_price REAL,
            target_id INTEGER NOT NULL REFERENCES publishing_targets(id),
            priority INTEGER NOT NULL DEFAULT 100,
            enabled INTEGER NOT NULL DEFAULT 1,
            stop_on_match INTEGER NOT NULL DEFAULT 1,
            filter_field TEXT NOT NULL DEFAULT '', filter_value TEXT NOT NULL DEFAULT '',
            delivery_mode TEXT NOT NULL DEFAULT 'auto'
        );
        CREATE TABLE IF NOT EXISTS publishing_deliveries (
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES publishing_targets(id),
            status TEXT NOT NULL, telegram_message_id INTEGER,
            error TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY(message_id, target_id)
        );
        CREATE TABLE IF NOT EXISTS publishing_resends (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES publishing_targets(id),
            requested_by INTEGER,
            status TEXT NOT NULL,
            telegram_message_id INTEGER,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_publishing_deliveries_status ON publishing_deliveries(status);
        CREATE INDEX IF NOT EXISTS idx_publishing_resends_target
            ON publishing_resends(target_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_publishing_resends_status ON publishing_resends(status);
        """)
        target_columns = {row[1] for row in conn.execute("PRAGMA table_info(publishing_targets)")}
        for name, definition in {"description": "TEXT NOT NULL DEFAULT ''",
                                 "connection_status": "TEXT NOT NULL DEFAULT 'unknown'",
                                 "connection_detail": "TEXT NOT NULL DEFAULT ''",
                                 "checked_at": "TEXT", "rate_limited_until": "TEXT"}.items():
            if name not in target_columns:
                conn.execute(f"ALTER TABLE publishing_targets ADD COLUMN {name} {definition}")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(publishing_rules)")}
        for name, definition in {
            "source_channel": "TEXT NOT NULL DEFAULT ''",
            "origin_city": "TEXT NOT NULL DEFAULT ''",
            "destination_city": "TEXT NOT NULL DEFAULT ''",
            "min_price": "REAL", "max_price": "REAL",
            "filter_field": "TEXT NOT NULL DEFAULT ''", "filter_value": "TEXT NOT NULL DEFAULT ''",
            "delivery_mode": "TEXT NOT NULL DEFAULT 'auto'",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE publishing_rules ADD COLUMN {name} {definition}")
        conn.commit()
    finally:
        conn.close()


def list_targets():
    initialize()
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM publishing_targets ORDER BY id")]
    finally:
        conn.close()


def list_rules():
    initialize()
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute("""SELECT r.*, t.label AS target_label, t.chat_id
            FROM publishing_rules r JOIN publishing_targets t ON t.id=r.target_id
            ORDER BY r.priority, r.id""")]
    finally:
        conn.close()


def distinct_filter_values(category, field, limit=120):
    """Read stored category values for a rule dropdown; identifiers are whitelisted."""
    if category not in FILTER_FIELDS or field not in FILTER_FIELDS[category]:
        raise ValueError("Invalid category field")
    conn = get_connection()
    try:
        rows = conn.execute(f"""SELECT DISTINCT TRIM(CAST(c.{field} AS TEXT)) AS value
            FROM {category} c
            WHERE c.{field} IS NOT NULL
              AND LENGTH(TRIM(CAST(c.{field} AS TEXT))) BETWEEN 1 AND 80
            ORDER BY value COLLATE NOCASE LIMIT ?""", (min(max(int(limit), 1), 200),))
        return [row["value"] for row in rows]
    finally:
        conn.close()


def save_target(label, chat_id, enabled=True, target_id=None, description=""):
    label, chat_id = label.strip(), chat_id.strip()
    description = str(description or "").strip()[:500]
    if not label or not (re.fullmatch(r'@[A-Za-z0-9_]{5,32}', chat_id) or
                         re.fullmatch(r'-[0-9]{5,}', chat_id)):
        raise ValueError("A label and a @channel username or negative Telegram chat ID are required")
    initialize()
    conn = get_connection()
    try:
        if target_id is None:
            conn.execute("INSERT INTO publishing_targets(label,chat_id,enabled,description) VALUES(?,?,?,?)",
                         (label, chat_id, int(enabled), description))
        else:
            cursor = conn.execute("""UPDATE publishing_targets SET label=?,chat_id=?,enabled=?,description=?,
                connection_status=CASE WHEN chat_id=? THEN connection_status ELSE 'unknown' END,
                connection_detail=CASE WHEN chat_id=? THEN connection_detail ELSE '' END,
                checked_at=CASE WHEN chat_id=? THEN checked_at ELSE NULL END WHERE id=?""",
                (label, chat_id, int(enabled), description, chat_id, chat_id, chat_id, int(target_id)))
            if not cursor.rowcount:
                raise ValueError("Target not found")
        conn.commit()
    finally:
        conn.close()


def save_rule(name, category, country, scope, target_id, priority=100,
              enabled=True, stop_on_match=True, rule_id=None, source_channel="",
              origin_city="", destination_city="", min_price=None, max_price=None,
              filter_field="", filter_value="", delivery_mode="auto"):
    name, country = name.strip(), country.strip().upper()
    if not name or category not in CATEGORIES or scope not in SCOPES:
        raise ValueError("Invalid rule name, category, or country condition")
    if country and (len(country) != 2 or not country.isascii() or not country.isalpha()):
        raise ValueError("Country must be an ISO 3166-1 two-letter code, such as TR")
    source_channel = str(source_channel or "").strip().lstrip("@").casefold()
    origin_city = str(origin_city or "").strip().casefold()
    destination_city = str(destination_city or "").strip().casefold()
    filter_field = str(filter_field or "").strip()
    filter_value = str(filter_value or "").strip().casefold()
    if filter_field not in ("", *FILTER_FIELDS[category]) or bool(filter_value) != bool(filter_field):
        raise ValueError("Invalid category filter")
    if delivery_mode not in {"auto", "manual"}:
        raise ValueError("Invalid delivery mode")
    min_price = float(min_price) if min_price not in (None, "") else None
    max_price = float(max_price) if max_price not in (None, "") else None
    if (min_price is not None and (not math.isfinite(min_price) or min_price < 0) or
            max_price is not None and (not math.isfinite(max_price) or max_price < 0) or
            min_price is not None and max_price is not None and min_price > max_price):
        raise ValueError("Invalid price range")
    initialize()
    conn = get_connection()
    try:
        target = conn.execute("SELECT id FROM publishing_targets WHERE id=?", (int(target_id),)).fetchone()
        if not target:
            raise ValueError("Target not found")
        values = (name, category, country, scope, source_channel, origin_city,
                  destination_city, min_price, max_price, int(target_id), int(priority),
                  int(enabled), int(stop_on_match), filter_field, filter_value, delivery_mode)
        if rule_id is None:
            conn.execute("""INSERT INTO publishing_rules
                (name,category,country,country_scope,source_channel,origin_city,
                destination_city,min_price,max_price,target_id,priority,enabled,stop_on_match,
                filter_field,filter_value,delivery_mode)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        else:
            cursor = conn.execute("""UPDATE publishing_rules SET name=?,category=?,country=?,
                country_scope=?,source_channel=?,origin_city=?,destination_city=?,
                min_price=?,max_price=?,target_id=?,priority=?,enabled=?,stop_on_match=?,
                filter_field=?,filter_value=?,delivery_mode=? WHERE id=?""",
                values + (int(rule_id),))
            if not cursor.rowcount:
                raise ValueError("Rule not found")
        conn.commit()
    finally:
        conn.close()


def delete_rule(rule_id, target_id):
    initialize()
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM publishing_rules WHERE id=? AND target_id=?",
                              (int(rule_id), int(target_id)))
        if cursor.rowcount != 1:
            raise ValueError("Rule not found for this channel")
        conn.commit()
    finally:
        conn.close()


def set_rate_limit(seconds):
    """Telegram rate limits apply to the bot; pause all destinations persistently."""
    seconds = min(max(int(seconds), 1), 3600)
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds + 1)).isoformat()
    initialize()
    conn = get_connection()
    try:
        conn.execute("UPDATE publishing_targets SET rate_limited_until=?", (until,))
        conn.commit()
    finally:
        conn.close()


def is_rate_limited():
    initialize()
    conn = get_connection()
    try:
        return conn.execute("SELECT 1 FROM publishing_targets WHERE rate_limited_until>? LIMIT 1",
                            (_now_iso(),)).fetchone() is not None
    finally:
        conn.close()


def mark_incomplete_deliveries():
    """Surface interrupted sends for human review; Telegram has no send idempotency key."""
    initialize()
    conn = get_connection()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        now = _now_iso()
        conn.execute("""UPDATE publishing_deliveries SET status='uncertain',
            error='Send interrupted. Check the channel before retrying; it may have been delivered.',
            updated_at=? WHERE status='sending' AND updated_at<?""", (now, cutoff))
        conn.execute("""UPDATE publishing_resends SET status='uncertain',
            error='Manual resend interrupted. Check the channel before sending another copy.',
            updated_at=? WHERE status='sending' AND updated_at<?""", (now, cutoff))
        conn.commit()
    finally:
        conn.close()


def set_enabled(table, item_id, enabled):
    if table not in {"publishing_rules", "publishing_targets"}:
        raise ValueError("Invalid table")
    conn = get_connection()
    try:
        conn.execute(f"UPDATE {table} SET enabled=? WHERE id=?", (int(enabled), int(item_id)))
        conn.commit()
    finally:
        conn.close()


def matching_targets(record, rules):
    """Rules run in priority order; first matching rule wins unless continuation is selected."""
    category = record.get("ai_category")
    seen = set()
    targets = []
    for rule in rules:
        if not rule["enabled"] or not rule["target_enabled"] or rule["category"] != category:
            continue
        country = rule["country"]
        if country:
            origin = str(record.get("origin_country") or record.get("country_code") or "").strip().upper()
            destination = str(record.get("destination_country") or "").strip().upper()
            scope = rule["country_scope"]
            if not ((scope in {"either", "origin"} and origin == country) or
                    (scope in {"either", "destination"} and destination == country)):
                continue
        if rule["source_channel"] and str(record.get("channel_username") or "").strip().lstrip("@").casefold() != rule["source_channel"]:
            continue
        if rule["origin_city"] and str(record.get("origin_city") or record.get("city") or "").strip().casefold() != rule["origin_city"]:
            continue
        if rule["destination_city"] and str(record.get("destination_city") or "").strip().casefold() != rule["destination_city"]:
            continue
        if rule.get("filter_field") and rule.get("filter_value"):
            value = record.get(rule["filter_field"])
            if str(value if value is not None else "").strip().casefold() != rule["filter_value"]:
                continue
        if rule["min_price"] is not None or rule["max_price"] is not None:
            try:
                price = float(record["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if rule["min_price"] is not None and price < rule["min_price"]:
                continue
            if rule["max_price"] is not None and price > rule["max_price"]:
                continue
        if rule["target_id"] not in seen:
            targets.append(rule)
            seen.add(rule["target_id"])
        if rule["stop_on_match"]:
            break
    return targets


def rule_matches(rule_id, limit=25, offset=0, message_id=None):
    """Count and preview ads matching one rule, including their delivery state."""
    mark_incomplete_deliveries()
    initialize()
    conn = get_connection()
    try:
        rule_row = conn.execute("""SELECT r.*,t.chat_id,t.enabled AS target_enabled,
            t.connection_status,t.rate_limited_until FROM publishing_rules r
            JOIN publishing_targets t ON t.id=r.target_id WHERE r.id=?""",
            (int(rule_id),)).fetchone()
        if rule_row is None:
            raise ValueError("Rule not found")
        rule = dict(rule_row)
        category = rule["category"]
        if category not in CATEGORIES:
            raise ValueError("Invalid category")
        # The category table is the source of truth for previews. Rows can exist there
        # before the message pipeline flips ai_status to "processed".
        where = []
        params = []
        if message_id is not None:
            where.append("m.id=?")
            params.append(int(message_id))
        if rule["country"]:
            origin = "COALESCE(NULLIF(c.origin_country,''),'')" if category == "transferlist" else (
                "COALESCE(c.country_code,'')" if category == "housinglist" else "''")
            dest = "COALESCE(c.destination_country,'')" if category == "transferlist" else "''"
            fields = [expression for scope, expression in (("origin", origin), ("destination", dest))
                      if rule["country_scope"] in ("either", scope)]
            where.append("(" + " OR ".join(f"UPPER(TRIM({expression}))=?" for expression in fields) + ")")
            params.extend([rule["country"]] * len(fields))
        if rule["source_channel"]:
            where.append("LOWER(LTRIM(TRIM(m.channel_username),'@'))=?")
            params.append(rule["source_channel"])
        if rule["origin_city"]:
            field = "origin_city" if category == "transferlist" else "city" if category == "housinglist" else None
            where.append(f"LOWER(TRIM(COALESCE(c.{field},'')))=?" if field else "0")
            if field: params.append(rule["origin_city"])
        if rule["destination_city"]:
            where.append("LOWER(TRIM(COALESCE(c.destination_city,'')))=?" if category == "transferlist" else "0")
            if category == "transferlist": params.append(rule["destination_city"])
        if rule["filter_field"] and rule["filter_value"]:
            if rule["filter_field"] not in FILTER_FIELDS[category]:
                raise ValueError("Invalid category filter")
            where.append(f"LOWER(TRIM(CAST(c.{rule['filter_field']} AS TEXT)))=?")
            params.append(rule["filter_value"])
        if rule["min_price"] is not None or rule["max_price"] is not None:
            if category == "joblist":
                where.append("0")
            else:
                if rule["min_price"] is not None:
                    where.append("c.price>=?")
                    params.append(rule["min_price"])
                if rule["max_price"] is not None:
                    where.append("c.price<=?")
                    params.append(rule["max_price"])
        joins = (f"FROM {category} c JOIN messages m ON m.id=c.processed_message_id "
                 "LEFT JOIN publishing_deliveries d ON d.message_id=m.id AND d.target_id=?")
        args = [rule["target_id"], *params]
        clause = " AND ".join(where) or "1"
        counts = conn.execute(f"""SELECT COUNT(*) AS total,
            COUNT(CASE WHEN d.status='sent' THEN 1 END) AS sent
            {joins} WHERE {clause}""", args).fetchone()
        rows = conn.execute(f"""SELECT m.id AS message_row_id,m.message_id AS telegram_source_id,
            m.*,c.*,d.status AS delivery_status
            {joins} WHERE {clause} ORDER BY m.id DESC LIMIT ? OFFSET ?""",
            [*args, min(max(int(limit), 1), 100), max(int(offset), 0)]).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            # A category-table row is sufficient to identify its formatter even when
            # messages.ai_category has not been finalized yet.
            record["ai_category"] = category
            records.append(record)
        return rule, dict(counts), records
    finally:
        conn.close()


def selected_pair(rule_id, message_id, allow_sent=False):
    rule, _, records = rule_matches(rule_id, limit=1, message_id=message_id)
    blocked = {"sending", "uncertain"}
    if not allow_sent:
        blocked.add("sent")
    if (rule["delivery_mode"] != "manual" or not rule["enabled"] or not rule["target_enabled"]
            or rule["connection_status"] == "disconnected" or not records
            or records[0]["delivery_status"] in blocked):
        raise ValueError("This ad is unavailable for manual publishing")
    if is_rate_limited():
        raise ValueError("Telegram rate limit active; wait before sending")
    return records[0], rule


def selected_resend_pair(rule_id, message_id):
    rule, _, records = rule_matches(rule_id, limit=1, message_id=message_id)
    if (not rule["enabled"] or not rule["target_enabled"]
            or rule["connection_status"] == "disconnected" or not records
            or records[0].get("delivery_status") != "sent"):
        raise ValueError("Only a delivered ad can be sent again")
    if is_rate_limited():
        raise ValueError("Telegram rate limit active; wait before sending")
    return records[0], rule


def claim_delivery(message_id, target_id):
    initialize()
    conn = get_connection()
    try:
        now = _now_iso()
        cursor = conn.execute("""INSERT INTO publishing_deliveries
            (message_id,target_id,status,telegram_message_id,error,updated_at)
            VALUES(?,?,'sending',NULL,NULL,?) ON CONFLICT(message_id,target_id) DO UPDATE SET
            status='sending',error=NULL,updated_at=excluded.updated_at
            WHERE publishing_deliveries.status IN ('retry','rejected')""",
            (int(message_id), int(target_id), now))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def claim_resend(message_id, target_id, requested_by=None):
    """Create one append-only manual resend attempt without changing the original delivery."""
    initialize()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute("""SELECT 1 FROM publishing_resends
            WHERE message_id=? AND target_id=? AND status='sending' LIMIT 1""",
            (int(message_id), int(target_id))).fetchone()
        if active is not None:
            conn.rollback()
            return None
        now = _now_iso()
        cursor = conn.execute("""INSERT INTO publishing_resends
            (message_id,target_id,requested_by,status,telegram_message_id,error,created_at,updated_at)
            VALUES(?,?,?,'sending',NULL,NULL,?,?)""",
            (int(message_id), int(target_id),
             int(requested_by) if requested_by is not None else None, now, now))
        resend_id = cursor.lastrowid
        conn.commit()
        return resend_id
    finally:
        conn.close()


def pending(limit=100):
    """Yield only unsent message-target pairs; disabled or unmatched rules publish nothing."""
    mark_incomplete_deliveries()
    initialize()
    if is_rate_limited():
        return []
    conn = get_connection()
    try:
        rules = [dict(row) for row in conn.execute("""SELECT r.*, t.chat_id,
            t.enabled AS target_enabled FROM publishing_rules r
            JOIN publishing_targets t ON t.id=r.target_id
            WHERE r.enabled=1 AND r.delivery_mode='auto' AND t.enabled=1
            AND t.connection_status!='disconnected'
            ORDER BY r.priority,r.id""")]
        if not rules:
            return []
        result = []
        offset = 0
        while len(result) < limit:
            rows = conn.execute("""SELECT id AS message_row_id, ai_category,channel_username,
                message_id AS telegram_source_id, sender_username
                FROM messages m WHERE ai_status='processed'
                AND ai_category IN ('transferlist','housinglist','joblist')
                AND EXISTS (SELECT 1 FROM publishing_rules r
                    JOIN publishing_targets target ON target.id=r.target_id
                    WHERE r.enabled=1 AND r.delivery_mode='auto' AND target.enabled=1
                    AND target.connection_status!='disconnected'
                    AND r.category=m.ai_category
                    AND NOT EXISTS (SELECT 1 FROM publishing_deliveries d
                        WHERE d.message_id=m.id AND d.target_id=r.target_id
                        AND d.status IN ('sent','rejected')))
                ORDER BY m.id LIMIT 200 OFFSET ?""", (offset,)).fetchall()
            if not rows:
                break
            offset += len(rows)
            for row in rows:
                record = dict(row)
                category = record["ai_category"]
                data = conn.execute(f"SELECT * FROM {category} WHERE processed_message_id=?",
                                    (record["message_row_id"],)).fetchone()
                if data is None:
                    continue
                record.update(dict(data))
                for rule in matching_targets(record, rules):
                    status = conn.execute("""SELECT status FROM publishing_deliveries
                        WHERE message_id=? AND target_id=?""",
                        (record["message_row_id"], rule["target_id"])).fetchone()
                    if status is None or status["status"] == "retry":
                        result.append((record, rule))
                        if len(result) >= limit:
                            break
                if len(result) >= limit:
                    break
        return result
    finally:
        conn.close()


def record_delivery(message_id, target_id, status, telegram_message_id=None, error=None):
    initialize()
    conn = get_connection()
    try:
        conn.execute("""INSERT INTO publishing_deliveries
            (message_id,target_id,status,telegram_message_id,error,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(message_id,target_id) DO UPDATE SET
            status=excluded.status,telegram_message_id=excluded.telegram_message_id,
            error=excluded.error,updated_at=excluded.updated_at""",
            (message_id, target_id, status, telegram_message_id, error,
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def record_resend(resend_id, status, telegram_message_id=None, error=None):
    initialize()
    conn = get_connection()
    try:
        cursor = conn.execute("""UPDATE publishing_resends SET status=?,telegram_message_id=?,
            error=?,updated_at=? WHERE id=?""",
            (status, telegram_message_id, error, _now_iso(), int(resend_id)))
        if cursor.rowcount != 1:
            raise ValueError("Manual resend attempt not found")
        conn.commit()
    finally:
        conn.close()


def recent_deliveries(limit=30, target_id=None):
    mark_incomplete_deliveries()
    initialize()
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute("""SELECT h.*, t.label AS target_label
            FROM (
                SELECT NULL AS resend_id,message_id,target_id,status,telegram_message_id,error,
                       updated_at,NULL AS requested_by,'original' AS delivery_kind
                FROM publishing_deliveries
                UNION ALL
                SELECT id AS resend_id,message_id,target_id,status,telegram_message_id,error,
                       updated_at,requested_by,'resend' AS delivery_kind
                FROM publishing_resends
            ) h JOIN publishing_targets t ON t.id=h.target_id
            WHERE (? IS NULL OR h.target_id=?)
            ORDER BY h.updated_at DESC LIMIT ?""", (target_id, target_id, int(limit)))]
    finally:
        conn.close()


def retry_delivery(message_id, target_id):
    initialize()
    conn = get_connection()
    try:
        conn.execute("""UPDATE publishing_deliveries SET status='retry',error=NULL,
            updated_at=? WHERE message_id=? AND target_id=? AND status IN ('rejected','retry','uncertain')""",
            (_now_iso(), int(message_id), int(target_id)))
        conn.commit()
    finally:
        conn.close()


def update_target_connection(target_id, status, detail):
    if status not in {"connected", "disconnected", "unknown"}:
        raise ValueError("Invalid connection status")
    initialize()
    conn = get_connection()
    try:
        conn.execute("""UPDATE publishing_targets SET connection_status=?,connection_detail=?,checked_at=?
            WHERE id=?""", (status, str(detail or "")[:500], _now_iso(), int(target_id)))
        if status == "connected":
            conn.execute("""UPDATE publishing_deliveries SET status='retry',error=NULL,updated_at=?
                WHERE target_id=? AND status='rejected' AND error LIKE '%Telegram sendMessage HTTP 403%'""",
                (_now_iso(), int(target_id)))
        conn.commit()
    finally:
        conn.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()
