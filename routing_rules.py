"""SQLite-backed publishing rules and per-destination delivery state."""
from datetime import datetime, timedelta, timezone
import json
import math
import re

from storage.database import CATEGORY_TABLES, get_connection
from storage.data_normalizer import initialize as initialize_normalization, normalize_category_data

# Categories follow the structured category tables used by the extraction pipeline.
# Rule fields themselves are discovered from SQLite so adding a column does not
# require a transport-specific rule change.
CATEGORIES = tuple(CATEGORY_TABLES)  # compatibility snapshot; runtime discovery uses categories()
SCOPES = ("either", "origin", "destination")  # legacy rule compatibility only
FILTER_OPERATORS = ("eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte",
                    "empty", "not_empty")
_NO_VALUE_OPERATORS = {"empty", "not_empty"}


def categories():
    """Discover structured topic tables from SQLite instead of hard-coding business topics."""
    conn = get_connection()
    try:
        names = []
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        for row in rows:
            name = row[0]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            columns = {column[1] for column in conn.execute(f"PRAGMA table_info({name})").fetchall()}
            if "processed_message_id" in columns:
                names.append(name)
        preferred = [name for name in CATEGORY_TABLES if name in names]
        preferred.extend(name for name in names if name not in preferred)
        return tuple(preferred)
    finally:
        conn.close()


def category_fields(category):
    """Return filterable fields from the real SQLite category table."""
    if category not in categories():
        raise ValueError("Invalid category")
    conn = get_connection()
    try:
        rows = conn.execute(f"PRAGMA table_info({category})").fetchall()
        return tuple(row[1] for row in rows if row[1] not in {"id", "processed_message_id"})
    finally:
        conn.close()


def filter_fields():
    """Return the current database-backed field map used by the back office."""
    return {category: category_fields(category) for category in categories()}


def _normalize_conditions(category, conditions):
    if conditions in (None, ""):
        return []
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid rule conditions") from exc
    if not isinstance(conditions, list) or len(conditions) > 12:
        raise ValueError("Rule conditions must be a list of at most 12 items")
    allowed = set(category_fields(category))
    normalized = []
    for index, item in enumerate(conditions):
        if not isinstance(item, dict):
            raise ValueError("Invalid rule condition")
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "eq").strip()
        join = "and" if index == 0 else str(item.get("join") or "and").strip().lower()
        value = "" if operator in _NO_VALUE_OPERATORS else str(item.get("value") or "").strip()
        if field not in allowed or operator not in FILTER_OPERATORS or join not in {"and", "or"}:
            raise ValueError("Invalid rule condition")
        if operator not in _NO_VALUE_OPERATORS and value == "":
            raise ValueError("Rule condition value is required")
        if operator in {"gt", "gte", "lt", "lte"}:
            try:
                number = float(value)
            except ValueError as exc:
                raise ValueError("Numeric rule condition requires a number") from exc
            if not math.isfinite(number):
                raise ValueError("Numeric rule condition requires a finite number")
            value = str(number)
        normalized.append({"field": field, "operator": operator, "value": value, "join": join})
    return normalized


def _legacy_conditions(rule):
    """Translate old transport-oriented columns into generic database conditions."""
    category = rule["category"]
    fields = set(category_fields(category))
    conditions = []

    def add(field, operator, value, join="and"):
        if field in fields and value not in (None, ""):
            conditions.append({"field": field, "operator": operator, "value": str(value),
                               "join": "and" if not conditions else join})

    country = str(rule.get("country") or "").strip()
    if country:
        scope = rule.get("country_scope") or "either"
        if category == "transferlist":
            if scope in {"either", "origin"}:
                add("origin_country", "eq", country)
            if scope in {"either", "destination"}:
                add("destination_country", "eq", country, "or" if scope == "either" else "and")
        elif category == "housinglist":
            add("country_code", "eq", country)
    origin_city = rule.get("origin_city")
    if origin_city:
        add("origin_city" if category == "transferlist" else "city", "eq", origin_city)
    if rule.get("destination_city"):
        add("destination_city", "eq", rule["destination_city"])
    if rule.get("min_price") is not None:
        add("price", "gte", rule["min_price"])
    if rule.get("max_price") is not None:
        add("price", "lte", rule["max_price"])
    if rule.get("filter_field") and rule.get("filter_value"):
        add(rule["filter_field"], "eq", rule["filter_value"])
    return conditions


def effective_conditions(rule):
    """Return normalized generic conditions, falling back to old rule columns."""
    raw = rule.get("conditions_json")
    if raw:
        try:
            conditions = _normalize_conditions(rule["category"], raw)
        except ValueError:
            conditions = []
        if conditions:
            return conditions
    return _legacy_conditions(rule)


def _record_condition_matches(record, condition):
    value = record.get(condition["field"])
    operator = condition["operator"]
    expected = condition["value"]
    text = "" if value is None else str(value).strip()
    if operator == "empty":
        return text == ""
    if operator == "not_empty":
        return text != ""
    if operator in {"gt", "gte", "lt", "lte"}:
        try:
            actual_number, expected_number = float(value), float(expected)
        except (TypeError, ValueError):
            return False
        return {"gt": actual_number > expected_number, "gte": actual_number >= expected_number,
                "lt": actual_number < expected_number, "lte": actual_number <= expected_number}[operator]
    actual_fold, expected_fold = text.casefold(), str(expected).strip().casefold()
    if operator == "eq":
        return actual_fold == expected_fold
    if operator == "ne":
        return actual_fold != expected_fold
    if operator == "contains":
        return expected_fold in actual_fold
    if operator == "not_contains":
        return expected_fold not in actual_fold
    return False


def _record_matches_conditions(record, conditions):
    result = None
    for condition in conditions:
        current = _record_condition_matches(record, condition)
        if result is None:
            result = current
        elif condition.get("join") == "or":
            result = result or current
        else:
            result = result and current
    return True if result is None else result


def _condition_sql(condition):
    field = condition["field"]
    operator = condition["operator"]
    value = condition["value"]
    column = f"c.{field}"
    if operator == "empty":
        return f"TRIM(COALESCE(CAST({column} AS TEXT),''))=''", []
    if operator == "not_empty":
        return f"TRIM(COALESCE(CAST({column} AS TEXT),''))<>''", []
    if operator in {"gt", "gte", "lt", "lte"}:
        sql_op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
        return f"CAST({column} AS REAL){sql_op}?", [float(value)]
    normalized = f"LOWER(TRIM(COALESCE(CAST({column} AS TEXT),'')))"
    if operator == "eq":
        return f"{normalized}=?", [str(value).casefold()]
    if operator == "ne":
        return f"{normalized}<>?", [str(value).casefold()]
    if operator == "contains":
        return f"{normalized} LIKE ?", [f"%{str(value).casefold()}%"]
    if operator == "not_contains":
        return f"{normalized} NOT LIKE ?", [f"%{str(value).casefold()}%"]
    raise ValueError("Invalid rule condition")


def _conditions_sql(conditions):
    expression = ""
    params = []
    for condition in conditions:
        part, values = _condition_sql(condition)
        expression = part if not expression else (
            f"({expression} {condition.get('join', 'and').upper()} {part})")
        params.extend(values)
    return expression, params


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
            delivery_mode TEXT NOT NULL DEFAULT 'auto',
            conditions_json TEXT NOT NULL DEFAULT '[]'
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
            "conditions_json": "TEXT NOT NULL DEFAULT '[]'",
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
    """Read stored category values for suggestions; identifiers come from SQLite."""
    if category not in categories() or field not in category_fields(category):
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
              filter_field="", filter_value="", delivery_mode="auto", conditions=None):
    name, country = name.strip(), country.strip().upper()
    if not name or category not in categories() or scope not in SCOPES:
        raise ValueError("Invalid rule name, category, or country condition")
    if country and (len(country) != 2 or not country.isascii() or not country.isalpha()):
        raise ValueError("Country must be an ISO 3166-1 two-letter code, such as TR")
    source_channel = str(source_channel or "").strip().lstrip("@").casefold()
    origin_city = str(origin_city or "").strip().casefold()
    destination_city = str(destination_city or "").strip().casefold()
    filter_field = str(filter_field or "").strip()
    filter_value = str(filter_value or "").strip().casefold()
    if filter_field not in ("", *category_fields(category)) or bool(filter_value) != bool(filter_field):
        raise ValueError("Invalid category filter")
    normalized_conditions = _normalize_conditions(category, conditions)
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
        conditions_json = json.dumps(normalized_conditions, ensure_ascii=False, separators=(",", ":"))
        values = (name, category, country, scope, source_channel, origin_city,
                  destination_city, min_price, max_price, int(target_id), int(priority),
                  int(enabled), int(stop_on_match), filter_field, filter_value, delivery_mode,
                  conditions_json)
        if rule_id is None:
            conn.execute("""INSERT INTO publishing_rules
                (name,category,country,country_scope,source_channel,origin_city,
                destination_city,min_price,max_price,target_id,priority,enabled,stop_on_match,
                filter_field,filter_value,delivery_mode,conditions_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        else:
            cursor = conn.execute("""UPDATE publishing_rules SET name=?,category=?,country=?,
                country_scope=?,source_channel=?,origin_city=?,destination_city=?,
                min_price=?,max_price=?,target_id=?,priority=?,enabled=?,stop_on_match=?,
                filter_field=?,filter_value=?,delivery_mode=?,conditions_json=? WHERE id=?""",
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
        if rule["source_channel"] and str(record.get("channel_username") or "").strip().lstrip("@").casefold() != rule["source_channel"]:
            continue
        if not _record_matches_conditions(record, effective_conditions(rule)):
            continue
        if rule["target_id"] not in seen:
            targets.append(rule)
            seen.add(rule["target_id"])
        if rule["stop_on_match"]:
            break
    return targets


def rule_matches(rule_id, limit=25, offset=0, message_id=None):
    """Count and preview ads matching one database-driven rule, including delivery state."""
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
        if category not in categories():
            raise ValueError("Invalid category")
        conditions = effective_conditions(rule)
        where = []
        params = []
        if message_id is not None:
            where.append("m.id=?")
            params.append(int(message_id))
        if rule["source_channel"]:
            where.append("LOWER(LTRIM(TRIM(m.channel_username),'@'))=?")
            params.append(rule["source_channel"])
        condition_sql, condition_params = _conditions_sql(conditions)
        if condition_sql:
            where.append(condition_sql)
            params.extend(condition_params)
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
    initialize_normalization()
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
        active_categories = categories()
        if not active_categories:
            return []
        while len(result) < limit:
            placeholders = ",".join("?" for _ in active_categories)
            rows = conn.execute(f"""SELECT id AS message_row_id, ai_category,channel_username,
                message_id AS telegram_source_id, sender_username
                FROM messages m WHERE ai_status='processed'
                AND ai_category IN ({placeholders})
                AND EXISTS (SELECT 1 FROM publishing_rules r
                    JOIN publishing_targets target ON target.id=r.target_id
                    WHERE r.enabled=1 AND r.delivery_mode='auto' AND target.enabled=1
                    AND target.connection_status!='disconnected'
                    AND r.category=m.ai_category
                    AND NOT EXISTS (SELECT 1 FROM publishing_deliveries d
                        WHERE d.message_id=m.id AND d.target_id=r.target_id
                        AND d.status IN ('sent','rejected')))
                ORDER BY m.id LIMIT 200 OFFSET ?""", (*active_categories, offset)).fetchall()
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
                normalized_data, _ = normalize_category_data(category, dict(data), conn=conn)
                record.update(normalized_data)
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
