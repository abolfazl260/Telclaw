#!/usr/bin/env python3
"""Read-only aggregate audit of the three messages text columns.

Explicit invocation only; imports neither Telclaw config nor database initialization.
No content, identifying message metadata, or secrets are selected or printed.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


# Only a SELECT: do not add DDL, DML, migrations, or PRAGMAs to the audit.
_AUDIT_SQL = """
SELECT
    processing_status,
    COUNT(*) AS total_rows,
    SUM(CASE WHEN raw_text IS NULL THEN 1 ELSE 0 END) AS raw_text_null,
    SUM(CASE WHEN raw_text = '' THEN 1 ELSE 0 END) AS raw_text_empty,
    SUM(CASE WHEN cleaned_text IS NULL THEN 1 ELSE 0 END) AS cleaned_text_null,
    SUM(CASE WHEN cleaned_text = '' THEN 1 ELSE 0 END) AS cleaned_text_empty,
    SUM(CASE WHEN text IS NULL THEN 1 ELSE 0 END) AS text_null,
    SUM(CASE WHEN text = '' THEN 1 ELSE 0 END) AS text_empty,
    SUM(CASE WHEN processing_status = 'processed'
                   AND text IS NOT NULL AND cleaned_text IS NOT NULL
                   AND text <> cleaned_text
             THEN 1 ELSE 0 END) AS processed_text_cleaned_different,
    SUM(CASE WHEN processing_status = 'processed'
                   AND ((text IS NULL AND cleaned_text IS NOT NULL)
                        OR (text IS NOT NULL AND cleaned_text IS NULL))
             THEN 1 ELSE 0 END) AS processed_text_cleaned_one_null,
    SUM(CASE WHEN (processing_status IS NULL OR processing_status <> 'processed')
                   AND raw_text IS NOT NULL AND text IS NOT NULL
                   AND raw_text <> text
             THEN 1 ELSE 0 END) AS unprocessed_text_raw_different
FROM messages
GROUP BY processing_status
ORDER BY processing_status
"""

_METRICS = (
    "total_rows",
    "raw_text_null",
    "raw_text_empty",
    "cleaned_text_null",
    "cleaned_text_empty",
    "text_null",
    "text_empty",
    "processed_text_cleaned_different",
    "processed_text_cleaned_one_null",
    "unprocessed_text_raw_different",
)


def audit_database(database_path: str | Path) -> dict:
    """Return aggregate counts; never create or write the requested database."""
    path = Path(database_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database path must point to an existing SQLite file")

    # mode=ro prevents creating a missing DB and rejects database writes.
    # Use a local SQLite connection, not storage.database.get_connection(),
    # which may initialize/migrate schema in other call paths.
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_AUDIT_SQL).fetchall()

    by_status = []
    totals = {metric: 0 for metric in _METRICS}
    for row in rows:
        group = {"processing_status": row["processing_status"]}
        for metric in _METRICS:
            count = int(row[metric] or 0)
            group[metric] = count
            totals[metric] += count
        by_status.append(group)
    return {
        "summary": totals,
        "by_processing_status": by_status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect SQLite messages text-column integrity without changing data."
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Explicit path to an existing SQLite database (prefer a staging backup).",
    )
    args = parser.parse_args(argv)
    try:
        report = audit_database(args.database)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"Audit failed: {exc}\n")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
