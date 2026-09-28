"""Restricted, audited editing of Telclaw advertisement data in SQLite."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone

from storage.database import CATEGORY_TABLES, get_connection

TABLES = ("messages", "transferlist", "housinglist", "joblist")
MESSAGE_EDITABLE = frozenset({"text", "raw_text", "cleaned_text", "date", "channel_name",
                              "sender_username", "sender_type", "message_link"})
PAGE_SIZE = 25


class ConflictError(ValueError):
    """A cell changed or its row disappeared after it was opened for editing."""


def _table(table):
    if table not in TABLES:
        raise ValueError("Unknown data table")
    return table


def _columns(conn, table):
    return {row["name"]: row["type"].upper() for row in conn.execute(f"PRAGMA table_info({table})")}


def _editable(table, column, columns):
    return column in columns and (column in MESSAGE_EDITABLE if table == "messages"
                                  else column in CATEGORY_TABLES[table])


def initialize():
    conn = get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS backoffice_data_edits (
            id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL, table_name TEXT NOT NULL,
            row_id INTEGER NOT NULL, column_name TEXT NOT NULL, old_value TEXT,
            new_value TEXT, edited_at TEXT NOT NULL)""")
        conn.commit()
    finally:
        conn.close()


def page(table, page_number=1):
    table = _table(table)
    try:
        page_number = int(page_number)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid page") from exc
    if page_number < 1 or page_number > 1_000_000:
        raise ValueError("Invalid page")
    conn = get_connection()
    try:
        columns = _columns(conn, table)
        if "id" not in columns:
            raise ValueError("Data table unavailable")
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page_number = min(page_number, pages)
        rows = [dict(row) for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, (page_number - 1) * PAGE_SIZE))]
        return {"table": table, "columns": columns,
                "editable": [name for name in columns if _editable(table, name, columns)],
                "rows": rows, "total": total, "page": page_number, "pages": pages}
    finally:
        conn.close()


def cell(table, row_id, column):
    table = _table(table)
    try:
        row_id = int(row_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid row") from exc
    conn = get_connection()
    try:
        columns = _columns(conn, table)
        if not _editable(table, column, columns):
            raise ValueError("This column is read only")
        row = conn.execute(f"SELECT {column} AS value FROM {table} WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise ConflictError("Row no longer exists")
        return {"value": row["value"], "type": columns[column]}
    finally:
        conn.close()


def _convert(value, data_type, make_null):
    if make_null:
        return None
    value = str(value)
    if len(value) > 20000:
        raise ValueError("Cell value is too long (max 20,000 characters)")
    if data_type.startswith("INTEGER"):
        if not re.fullmatch(r"[+-]?\d+", value.strip()):
            raise ValueError("Enter a whole number or choose NULL")
        return int(value)
    if data_type.startswith(("REAL", "FLOAT", "NUMERIC")):
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError("Enter a number or choose NULL") from exc
        if not math.isfinite(number):
            raise ValueError("Number must be finite")
        return number
    return value


def update_cell(table, row_id, column, value, expected, admin_id, make_null=False):
    """Compare and update one cell atomically; preserve a per-cell audit trail."""
    table = _table(table)
    try:
        row_id, admin_id = int(row_id), int(admin_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid row or administrator") from exc
    initialize()
    conn = get_connection()
    try:
        columns = _columns(conn, table)
        if not _editable(table, column, columns):
            raise ValueError("This column is read only")
        new_value = _convert(value, columns[column], make_null)
        # SQLite IS matches NULL safely and compares other scalar values without coercing NULL.
        cursor = conn.execute(f"UPDATE {table} SET {column}=? WHERE id=? AND {column} IS ?",
                              (new_value, row_id, expected))
        if cursor.rowcount != 1:
            conn.rollback()
            raise ConflictError("Cell changed since you opened it. Reload before editing.")
        conn.execute("""INSERT INTO backoffice_data_edits
            (admin_id,table_name,row_id,column_name,old_value,new_value,edited_at)
            VALUES(?,?,?,?,?,?,?)""",
            (admin_id, table, row_id, column,
             json.dumps(expected, ensure_ascii=False), json.dumps(new_value, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return new_value
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Value conflicts with a database constraint") from exc
    finally:
        conn.close()
