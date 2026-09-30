"""Restricted, audited editing of Telclaw advertisement data in SQLite."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone

from storage.database import CATEGORY_TABLES, get_connection
from storage.data_normalizer import aliasable_fields, normalize_field_value, normalize_structured_field_value

TABLES = ("messages", "transferlist", "housinglist", "joblist")
MESSAGE_EDITABLE = frozenset({"text", "raw_text", "cleaned_text", "date", "channel_name",
                              "sender_username", "sender_type", "message_link"})
PAGE_SIZE = 25
TEXT_FILTER_OPERATORS = frozenset({
    "contains", "not_contains", "eq", "ne", "starts", "empty", "not_empty", "null", "not_null"
})
NUMERIC_FILTER_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "null", "not_null"})


def numeric_filter_type(data_type):
    """Return whether a SQLite declared type should use numeric filter operators."""
    upper = str(data_type or "").upper()
    return any(token in upper for token in ("INT", "REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL"))




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


def page(table, page_number=1, filters=None):
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
        clauses, params, active_filters = _filter_query(columns, filters)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page_number = min(page_number, pages)
        rows = [dict(row) for row in conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page_number - 1) * PAGE_SIZE])]
        return {"table": table, "columns": columns,
                "editable": [name for name in columns if _editable(table, name, columns)],
                "rows": rows, "total": total, "page": page_number, "pages": pages,
                "filters": active_filters}
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


def _convert_filter(value, data_type):
    value = str(value)
    if len(value) > 2000:
        raise ValueError("Filter value is too long")
    if numeric_filter_type(data_type):
        if "INT" in data_type:
            if not re.fullmatch(r"[+-]?\d+", value.strip()):
                raise ValueError("Numeric filter requires a whole number")
            return int(value)
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError("Numeric filter requires a number") from exc
        if not math.isfinite(number):
            raise ValueError("Numeric filter must be finite")
        return number
    return value


def _filter_query(columns, filters):
    clauses, params, active = [], [], {}
    for column, spec in (filters or {}).items():
        if column not in columns:
            raise ValueError("Unknown filter column")
        if not isinstance(spec, dict):
            raise ValueError("Invalid column filter")
        numeric = numeric_filter_type(columns[column])
        allowed = NUMERIC_FILTER_OPERATORS if numeric else TEXT_FILTER_OPERATORS
        default = "eq" if numeric else "contains"
        operator = str(spec.get("op") or default).strip()
        if operator not in allowed:
            raise ValueError("Invalid filter operator")
        value = spec.get("value", "")
        identifier = '"' + column.replace('"', '""') + '"'

        if operator in {"null", "not_null", "empty", "not_empty"}:
            if operator == "null":
                clauses.append(f"{identifier} IS NULL")
            elif operator == "not_null":
                clauses.append(f"{identifier} IS NOT NULL")
            elif operator == "empty":
                clauses.append(f"{identifier} = ''")
            else:
                clauses.append(f"{identifier} IS NOT NULL AND {identifier} <> ''")
            active[column] = {"op": operator, "value": ""}
            continue

        if value is None or str(value) == "":
            continue
        converted = _convert_filter(value, columns[column])
        if operator == "contains":
            clauses.append(f"instr(lower(CAST({identifier} AS TEXT)), lower(?)) > 0")
            params.append(converted)
        elif operator == "not_contains":
            clauses.append(f"({identifier} IS NULL OR instr(lower(CAST({identifier} AS TEXT)), lower(?)) = 0)")
            params.append(converted)
        elif operator == "starts":
            clauses.append(f"lower(substr(CAST({identifier} AS TEXT), 1, length(?))) = lower(?)")
            params.extend([converted, converted])
        else:
            sql_operator = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
            clauses.append(f"{identifier} {sql_operator} ?")
            params.append(converted)
        active[column] = {"op": operator, "value": str(value)}
    return clauses, params, active


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
        current_row = None
        if table in CATEGORY_TABLES:
            current_row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
            if current_row is None:
                raise ConflictError("Row no longer exists")

        if table == "housinglist" and column == "bathrooms":
            raw_value = None if make_null else value
            new_value = normalize_structured_field_value(table, column, raw_value, dict(current_row))
        else:
            new_value = _convert(value, columns[column], make_null)
            if table in CATEGORY_TABLES and column in aliasable_fields().get(table, ()):
                new_value = normalize_field_value(table, column, new_value, dict(current_row))
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
