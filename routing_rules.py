"""SQLite-backed publishing rules and per-destination delivery state."""
from datetime import datetime, timezone
import re

from storage.database import get_connection

CATEGORIES = ("transferlist", "housinglist", "joblist")
SCOPES = ("either", "origin", "destination")


def initialize():
    conn = get_connection()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS publishing_targets (
            id INTEGER PRIMARY KEY, label TEXT NOT NULL,
            chat_id TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS publishing_rules (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            category TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
            country_scope TEXT NOT NULL DEFAULT 'either',
            target_id INTEGER NOT NULL REFERENCES publishing_targets(id),
            priority INTEGER NOT NULL DEFAULT 100,
            enabled INTEGER NOT NULL DEFAULT 1,
            stop_on_match INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS publishing_deliveries (
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES publishing_targets(id),
            status TEXT NOT NULL, telegram_message_id INTEGER,
            error TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY(message_id, target_id)
        );
        CREATE INDEX IF NOT EXISTS idx_publishing_deliveries_status ON publishing_deliveries(status);
        """)
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


def save_target(label, chat_id, enabled=True, target_id=None):
    label, chat_id = label.strip(), chat_id.strip()
    if not label or not (re.fullmatch(r'@[A-Za-z0-9_]{5,32}', chat_id) or
                         re.fullmatch(r'-[0-9]{5,}', chat_id)):
        raise ValueError("A label and a @channel username or negative Telegram chat ID are required")
    initialize()
    conn = get_connection()
    try:
        if target_id is None:
            conn.execute("INSERT INTO publishing_targets(label,chat_id,enabled) VALUES(?,?,?)",
                         (label, chat_id, int(enabled)))
        else:
            cursor = conn.execute("UPDATE publishing_targets SET label=?,chat_id=?,enabled=? WHERE id=?",
                                  (label, chat_id, int(enabled), int(target_id)))
            if not cursor.rowcount:
                raise ValueError("Target not found")
        conn.commit()
    finally:
        conn.close()


def save_rule(name, category, country, scope, target_id, priority=100,
              enabled=True, stop_on_match=True, rule_id=None):
    name, country = name.strip(), country.strip().upper()
    if not name or category not in CATEGORIES or scope not in SCOPES:
        raise ValueError("Invalid rule name, category, or country condition")
    if country and (len(country) != 2 or not country.isascii() or not country.isalpha()):
        raise ValueError("Country must be an ISO 3166-1 two-letter code, such as TR")
    initialize()
    conn = get_connection()
    try:
        target = conn.execute("SELECT id FROM publishing_targets WHERE id=?", (int(target_id),)).fetchone()
        if not target:
            raise ValueError("Target not found")
        values = (name, category, country, scope, int(target_id), int(priority),
                  int(enabled), int(stop_on_match))
        if rule_id is None:
            conn.execute("""INSERT INTO publishing_rules
                (name,category,country,country_scope,target_id,priority,enabled,stop_on_match)
                VALUES(?,?,?,?,?,?,?,?)""", values)
        else:
            cursor = conn.execute("""UPDATE publishing_rules SET name=?,category=?,country=?,
                country_scope=?,target_id=?,priority=?,enabled=?,stop_on_match=? WHERE id=?""",
                values + (int(rule_id),))
            if not cursor.rowcount:
                raise ValueError("Rule not found")
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
        if rule["target_id"] not in seen:
            targets.append(rule)
            seen.add(rule["target_id"])
        if rule["stop_on_match"]:
            break
    return targets


def pending(limit=100):
    """Yield only unsent message-target pairs; disabled or unmatched rules publish nothing."""
    initialize()
    conn = get_connection()
    try:
        rules = [dict(row) for row in conn.execute("""SELECT r.*, t.chat_id,
            t.enabled AS target_enabled FROM publishing_rules r
            JOIN publishing_targets t ON t.id=r.target_id
            WHERE r.enabled=1 AND t.enabled=1 ORDER BY r.priority,r.id""")]
        if not rules:
            return []
        result = []
        offset = 0
        while len(result) < limit:
            rows = conn.execute("""SELECT id AS message_row_id, ai_category,
                message_id AS telegram_source_id, sender_username
                FROM messages WHERE ai_status='processed'
                AND ai_category IN ('transferlist','housinglist','joblist')
                ORDER BY id LIMIT 200 OFFSET ?""", (offset,)).fetchall()
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


def recent_deliveries(limit=30):
    initialize()
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute("""SELECT d.*, t.label AS target_label
            FROM publishing_deliveries d JOIN publishing_targets t ON t.id=d.target_id
            ORDER BY d.updated_at DESC LIMIT ?""", (int(limit),))]
    finally:
        conn.close()


def retry_delivery(message_id, target_id):
    initialize()
    conn = get_connection()
    try:
        conn.execute("""UPDATE publishing_deliveries SET status='retry',error=NULL,
            updated_at=? WHERE message_id=? AND target_id=? AND status IN ('rejected','retry')""",
            (_now_iso(), int(message_id), int(target_id)))
        conn.commit()
    finally:
        conn.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()
