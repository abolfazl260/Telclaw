#!/usr/bin/env python3
"""Opt-in, bounded reconciliation of historical message text in SQLite.

Default invocation is SELECT-only, with no schema initialization or network
access. --apply requires explicit limits, an expected candidate count, and
fresh backup/manifest destinations. Never changes source raw_text or legacy text.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

# Support direct execution: python scripts/reconcile_message_text.py ...
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processing.cleaner import clean_text


_REQUIRED_FIELDS = frozenset({
    "id", "processing_status", "raw_text", "text", "cleaned_text",
})
_EDIT_FIELDS = frozenset({
    "table_name", "row_id", "column_name",
})
_EDIT_COLUMNS = ("text", "raw_text", "cleaned_text")
_MAX_APPLY = 500
_EXAMPLE_LIMIT = 10


def _path_to_existing_db(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("The database must be an existing regular SQLite file")
    return resolved


def _connect(db_path: Path, *, write=False) -> sqlite3.Connection:
    mode = "rw" if write else "ro"
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, name: str) -> set[str]:
    # Read-only PRAGMA table-valued function: does not initialize/migrate DBs.
    return {
        row[0]
        for row in conn.execute("SELECT name FROM pragma_table_info(?)", (name,))
    }


def _has_edit_audit(conn: sqlite3.Connection) -> bool:
    return _EDIT_FIELDS.issubset(_columns(conn, "backoffice_data_edits"))


def _ensure_schema(conn: sqlite3.Connection) -> bool:
    missing = _REQUIRED_FIELDS - _columns(conn, "messages")
    if missing:
        raise ValueError("Unsupported messages schema; required text/status fields missing")
    return _has_edit_audit(conn)


def _select_rows(conn: sqlite3.Connection, edit_audit: bool):
    edit_check = (
        """EXISTS(SELECT 1 FROM backoffice_data_edits e
           WHERE e.table_name='messages' AND e.row_id=m.id
           AND e.column_name IN ('raw_text','text','cleaned_text'))"""
        if edit_audit else "0"
    )
    return conn.execute(
        f"""SELECT m.id, m.processing_status, m.raw_text, m.text, m.cleaned_text,
                 {edit_check} AS manually_edited
            FROM messages m ORDER BY m.id"""
    )


def _reason(row, edit_audit: bool) -> str:
    if row["processing_status"] != "processed":
        return "not_processed"
    raw = row["raw_text"]
    if raw is None:
        return "raw_null"
    if not isinstance(raw, str):
        return "raw_not_text"
    if raw == "" or clean_text(raw) == "":
        return "raw_empty_or_whitespace"
    if not edit_audit:
        return "edit_history_unavailable"
    if row["manually_edited"]:
        return "manual_text_edit_recorded"
    cleaned = row["cleaned_text"]
    if cleaned is not None:
        if cleaned == row["text"]:
            return "already_cleaned"
        return "existing_cleaned_mismatch"
    legacy = row["text"]
    if legacy is None:
        return "legacy_text_null"
    if not isinstance(legacy, str):
        return "legacy_text_not_text"
    if legacy == "":
        return "legacy_text_empty"
    if legacy != clean_text(raw):
        return "legacy_text_not_derived_from_raw"
    return "eligible_missing_cleaned"


def _scan(conn: sqlite3.Connection, *, batch_size=100, collect=False):
    edit_audit = _ensure_schema(conn)
    total = 0
    counts = Counter()
    statuses = defaultdict(Counter)
    examples = []
    candidates = []
    cursor = _select_rows(conn, edit_audit)
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        for row in batch:
            total += 1
            reason = _reason(row, edit_audit)
            counts[reason] += 1
            status = row["processing_status"]
            statuses[status][reason] += 1
            statuses[status]["total_rows"] += 1
            for field in ("raw_text", "text", "cleaned_text"):
                if row[field] is None:
                    statuses[status][f"{field}_null"] += 1
                elif row[field] == "":
                    statuses[status][f"{field}_empty"] += 1
            if reason == "eligible_missing_cleaned":
                if len(examples) < _EXAMPLE_LIMIT:
                    examples.append(row["id"])
                if collect:
                    if len(candidates) >= _MAX_APPLY:
                        # Continue counting without retaining unbounded user text.
                        continue
                    candidates.append({
                        "id": row["id"],
                        "raw_text": row["raw_text"],
                        "text": row["text"],
                        "expected_cleaned": clean_text(row["raw_text"]),
                    })
    report = {
        "read_only": True,
        "total_rows": total,
        "eligible_rows": counts["eligible_missing_cleaned"],
        "edit_history_available": edit_audit,
        "reason_counts": dict(sorted(counts.items())),
        "by_processing_status": [
            {"processing_status": status, **dict(sorted(metrics.items()))}
            for status, metrics in sorted(
                statuses.items(), key=lambda item: (item[0] is not None, str(item[0]))
            )
        ],
        "example_eligible_ids": examples,
    }
    return report, candidates


def audit_database(database_path: str | Path, *, batch_size=100) -> dict:
    """Read-only scan; returns counts and <=10 IDs, never message bodies."""
    if not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be from 1 to 500")
    path = _path_to_existing_db(database_path)
    with closing(_connect(path)) as conn:
        report, _ = _scan(conn, batch_size=batch_size)
    return report


def _fresh_private_path(path: Path, reserved: set[Path]) -> Path:
    dest = path.expanduser().resolve(strict=False)
    if dest in reserved:
        raise ValueError("Database, backup and manifest must have distinct paths")
    if not dest.parent.is_dir():
        raise ValueError("Backup/manifest parent directory must already exist")
    if dest.exists() or dest.is_symlink():
        raise FileExistsError("Backup/manifest destination must not already exist")
    return dest


def _private_create(dest: Path) -> None:
    # Exclusive, mode 0600, no overwrite and no symlink traversal.
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


def _backup_database(source: Path, destination: Path) -> None:
    """Create a consistent SQLite snapshot without deleting or replacing files."""
    _private_create(destination)
    with closing(_connect(source)) as source_conn:
        with closing(sqlite3.connect(destination)) as backup_conn:
            source_conn.backup(backup_conn)
            if backup_conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite backup failed integrity verification")
            _ensure_schema(backup_conn)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ValueError("SQLite backup is unexpectedly empty")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_event(stream, event: dict) -> None:
    stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def reconcile_database(
    database_path: str | Path,
    *,
    backup_path: str | Path,
    manifest_path: str | Path,
    expected_candidates: int,
    max_updates: int,
    batch_size: int = 100,
) -> dict:
    """Apply a bounded, CAS-protected correction after backup + manifest.

    At most one SQLite transaction changes only missing cleaned_text cells.
    The private JSONL manifest contains row IDs and SHA-256 digests, never text.
    """
    if not isinstance(expected_candidates, int) or expected_candidates < 1:
        raise ValueError("Supply a positive --expect-candidates from a prior dry-run")
    if not isinstance(max_updates, int) or not 1 <= max_updates <= _MAX_APPLY:
        raise ValueError("--max-updates must be between 1 and 500")
    if not 1 <= batch_size <= 500:
        raise ValueError("--batch-size must be from 1 to 500")
    source = _path_to_existing_db(database_path)
    backup = _fresh_private_path(Path(backup_path), {source})
    manifest = _fresh_private_path(Path(manifest_path), {source, backup})

    with closing(_connect(source)) as conn:
        report, candidates = _scan(conn, batch_size=batch_size, collect=True)
    total = report["eligible_rows"]
    if total != expected_candidates:
        raise ValueError("Eligible count changed since dry-run; rerun the audit")
    if total > max_updates:
        raise ValueError("Eligible count exceeds --max-updates; refusing partial updates")
    if not report["edit_history_available"]:
        raise ValueError("Edit history unavailable; refusing to correct uncertain rows")

    _backup_database(source, backup)

    # Verify the backed-up snapshot matches the scan before using it for
    # rollback. A concurrent write after the snapshot is handled by per-row CAS.
    with closing(_connect(backup)) as copied:
        snapshot_report, snapshot_candidates = _scan(
            copied, batch_size=batch_size, collect=True
        )
    if (
        snapshot_report["eligible_rows"] != total
        or [c["id"] for c in snapshot_candidates] != [c["id"] for c in candidates]
    ):
        raise ValueError("Backup is not the audited snapshot; no changes applied")

    _private_create(manifest)
    with open(manifest, "w", encoding="utf-8") as log:
        _write_event(log, {
            "event": "prepared",
            "utc": datetime.now(timezone.utc).isoformat(),
            "backup_path": str(backup),
            "eligible_count": total,
            "entries": [
                {
                    "id": candidate["id"],
                    "derived_cleaned_sha256": _hash_text(candidate["expected_cleaned"]),
                }
                for candidate in candidates
            ],
        })
        applied = []
        skipped = []
        with closing(_connect(source, write=True)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not _ensure_schema(conn):
                    raise ValueError("Edit history became unavailable; aborting")
                for start in range(0, len(candidates), batch_size):
                    for candidate in candidates[start : start + batch_size]:
                        row = conn.execute(
                            """SELECT m.id,m.processing_status,m.raw_text,m.text,
                                      m.cleaned_text,
                                      EXISTS(SELECT 1 FROM backoffice_data_edits e
                                         WHERE e.table_name='messages' AND e.row_id=m.id
                                         AND e.column_name IN
                                             ('raw_text','text','cleaned_text'))
                                      AS manually_edited
                               FROM messages m WHERE m.id=?""",
                            (candidate["id"],),
                        ).fetchone()
                        if (
                            row is None
                            or _reason(row, True) != "eligible_missing_cleaned"
                            or row["raw_text"] != candidate["raw_text"]
                            or row["text"] != candidate["text"]
                        ):
                            skipped.append(candidate["id"])
                            continue
                        cursor = conn.execute(
                            """UPDATE messages SET cleaned_text=?
                               WHERE id=? AND processing_status='processed'
                                 AND raw_text IS ? AND text IS ?
                                 AND cleaned_text IS NULL""",
                            (
                                candidate["expected_cleaned"], candidate["id"],
                                candidate["raw_text"], candidate["text"],
                            ),
                        )
                        if cursor.rowcount == 1:
                            applied.append(candidate["id"])
                        else:
                            skipped.append(candidate["id"])
                conn.commit()
            except Exception:
                conn.rollback()
                _write_event(log, {"event": "rolled_back", "utc": datetime.now(timezone.utc).isoformat()})
                raise
        _write_event(log, {
            "event": "committed",
            "utc": datetime.now(timezone.utc).isoformat(),
            "applied_ids": applied,
            "skipped_ids": skipped,
        })
    return {
        "backup_created": True,
        "manifest_created": True,
        "eligible_at_dry_run": total,
        "applied": len(applied),
        "skipped_due_to_concurrent_changes": len(skipped),
        "applied_ids": applied[:_EXAMPLE_LIMIT],
        "skipped_ids": skipped[:_EXAMPLE_LIMIT],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly reconcile missing cleaned_text in historical messages."
    )
    parser.add_argument("--database", type=Path, required=True, help="Existing SQLite file")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="Opt in to bounded changes")
    parser.add_argument("--backup", type=Path, help="NEW SQLite backup path (created privately)")
    parser.add_argument("--manifest", type=Path, help="NEW private JSONL change manifest path")
    parser.add_argument("--expect-candidates", type=int)
    parser.add_argument("--max-updates", type=int)
    args = parser.parse_args(argv)
    if args.apply:
        if any(item is None for item in (
            args.backup, args.manifest, args.expect_candidates, args.max_updates
        )):
            parser.error("--apply requires --backup, --manifest, --expect-candidates and --max-updates")
    elif any(item is not None for item in (
        args.backup, args.manifest, args.expect_candidates, args.max_updates
    )):
        parser.error("Backup, manifest and write limits are valid only with --apply")
    try:
        if args.apply:
            result = reconcile_database(
                args.database,
                backup_path=args.backup,
                manifest_path=args.manifest,
                expected_candidates=args.expect_candidates,
                max_updates=args.max_updates,
                batch_size=args.batch_size,
            )
        else:
            result = audit_database(args.database, batch_size=args.batch_size)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"Reconciliation refused: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
