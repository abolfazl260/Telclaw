"""Regression coverage for the opt-in historical text reconciliation script.

Fixture-only SQLite. Tests deliberately verify no message body leaks to reports,
no automatic modifications, atomic apply, edit-history skips and backup recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from processing.cleaner import clean_text
from scripts import reconcile_message_text as tool


PRIVATE_PAYLOAD = "  SECRET-PHONE-555   Flight 🧳 \n to Toronto  "


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "historical.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """CREATE TABLE messages(
                id INTEGER PRIMARY KEY, processing_status TEXT,
                raw_text TEXT, text TEXT, cleaned_text TEXT,
                ai_status TEXT DEFAULT 'waiting', advertio_status TEXT DEFAULT 'waiting'
            );
            CREATE TABLE backoffice_data_edits(
                id INTEGER PRIMARY KEY, table_name TEXT, row_id INTEGER,
                column_name TEXT
            );
            CREATE TABLE publishing_deliveries(
                id INTEGER PRIMARY KEY, status TEXT
            );
            CREATE TABLE transferlist(
                id INTEGER PRIMARY KEY, origin_city TEXT
            );
            INSERT INTO publishing_deliveries VALUES(1, 'sent');
            INSERT INTO transferlist VALUES(1, 'Tehran');"""
        )
        conn.executemany(
            "INSERT INTO messages(id,processing_status,raw_text,text,cleaned_text) "
            "VALUES(?,?,?,?,?)",
            [
                (1, "processed", PRIVATE_PAYLOAD, clean_text(PRIVATE_PAYLOAD), None),
                (2, "processed", "Already clean", "Already clean", "Already clean"),
                (3, "processed", "Source text", "Edited legacy", None),
                (4, "processed", "Manually edited", "Manually edited", None),
                (5, "pending", "Pending raw", "Pending raw", None),
                (6, "processed", None, "only legacy", None),
                (7, "processed", "", "", None),
                (8, "processed", "Existing empty", "Existing empty", ""),
                (9, "processed", "Source", None, None),
                (10, "processed", " Clean  me  ", "Clean me", None),
                (11, None, "Unknown status", "Unknown status", None),
                (12, "failed", "Failed  source", "Failed  source", ""),
            ],
        )
        conn.execute(
            "INSERT INTO backoffice_data_edits(table_name,row_id,column_name) "
            "VALUES('messages',4,'raw_text')"
        )
        conn.commit()
    return path


def snapshot(path: Path):
    with sqlite3.connect(path) as conn:
        messages = conn.execute(
            "SELECT * FROM messages ORDER BY id"
        ).fetchall()
        deliveries = conn.execute("SELECT * FROM publishing_deliveries").fetchall()
        transfers = conn.execute("SELECT * FROM transferlist").fetchall()
    return messages, deliveries, transfers


def kwargs(tmp_path, db_path, **extra):
    params = {
        "backup_path": tmp_path / "backup.sqlite3",
        "manifest_path": tmp_path / "changes.jsonl",
        "expected_candidates": 2,
        "max_updates": 2,
        "batch_size": 1,
    }
    params.update(extra)
    return params


def test_dry_run_is_read_only_and_reports_aggregate_categories_without_text(db_path, capsys):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    report = tool.audit_database(db_path, batch_size=1)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert report["read_only"] is True
    assert report["total_rows"] == 12
    assert report["eligible_rows"] == 2
    assert report["example_eligible_ids"] == [1, 10]
    assert report["edit_history_available"] is True
    assert report["reason_counts"]["manual_text_edit_recorded"] == 1
    assert report["reason_counts"]["legacy_text_not_derived_from_raw"] == 1
    assert report["reason_counts"]["raw_null"] == 1
    assert report["reason_counts"]["raw_empty_or_whitespace"] == 1
    assert report["reason_counts"]["legacy_text_null"] == 1
    assert report["reason_counts"]["existing_cleaned_mismatch"] == 1
    assert report["reason_counts"]["not_processed"] == 3
    assert tool.main(["--database", str(db_path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["eligible_rows"] == 2
    for secret in ("SECRET-PHONE-555", "Flight 🧳", "only legacy", "Edited legacy"):
        assert secret not in output


def test_explicit_apply_creates_verified_private_backup_and_nonplaintext_manifest(
    tmp_path, db_path
):
    before = snapshot(db_path)
    params = kwargs(tmp_path, db_path)
    result = tool.reconcile_database(db_path, **params)
    assert result["applied"] == 2
    assert result["skipped_due_to_concurrent_changes"] == 0
    assert result["applied_ids"] == [1, 10]
    assert params["backup_path"].exists()
    assert params["manifest_path"].exists()
    if os.name == "posix":
        assert params["backup_path"].stat().st_mode & 0o777 == 0o600
        assert params["manifest_path"].stat().st_mode & 0o777 == 0o600

    with sqlite3.connect(params["backup_path"]) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert snapshot(params["backup_path"]) == before

    after = snapshot(db_path)
    for prior, current in zip(before[0], after[0]):
        if prior[0] in (1, 10):
            assert prior[:4] == current[:4]
            assert prior[4] is None
            assert current[4] == clean_text(prior[2])
            assert prior[5:] == current[5:]
        else:
            assert current == prior
    assert after[1:] == before[1:]  # Delivery and category tables unchanged.

    manifest = params["manifest_path"].read_text(encoding="utf-8")
    events = [json.loads(line) for line in manifest.splitlines()]
    assert [event["event"] for event in events] == ["prepared", "committed"]
    assert events[1]["applied_ids"] == [1, 10]
    assert len(events[0]["entries"]) == 2
    assert "derived_cleaned_sha256" in events[0]["entries"][0]
    assert "SECRET-PHONE-555" not in manifest
    assert "Flight" not in manifest
    assert "Clean me" not in manifest

    second = tool.audit_database(db_path)
    assert second["eligible_rows"] == 0  # Idempotent.
    with pytest.raises(ValueError, match="Eligible count changed"):
        tool.reconcile_database(
            db_path, **kwargs(
                tmp_path, db_path,
                backup_path=tmp_path / "unused.sqlite3",
                manifest_path=tmp_path / "unused.jsonl",
            )
        )


def test_rollback_restores_historical_snapshot_without_deleting_any_records(
    tmp_path, db_path
):
    before = snapshot(db_path)
    params = kwargs(tmp_path, db_path)
    tool.reconcile_database(db_path, **params)

    # Recovery is an operator action against an OFFLINE NEW destination, not an
    # automatic in-place overwrite. This checks backup completeness.
    restored_path = tmp_path / "restored-to-new-file.sqlite3"
    with sqlite3.connect(params["backup_path"]) as saved:
        with sqlite3.connect(restored_path) as target:
            saved.backup(target)
    assert snapshot(restored_path) == before
    assert snapshot(db_path) != before
    assert params["backup_path"].exists()


def test_missing_edit_audit_skips_uncertain_rows_and_never_applies(tmp_path, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE backoffice_data_edits")
        conn.commit()
    report = tool.audit_database(db_path)
    assert report["edit_history_available"] is False
    assert report["eligible_rows"] == 0
    assert report["reason_counts"]["edit_history_unavailable"] >= 2

    with pytest.raises(ValueError, match="Eligible count changed"):
        tool.reconcile_database(
            db_path, **kwargs(
                tmp_path, db_path, expected_candidates=2,
            )
        )
    assert not (tmp_path / "backup.sqlite3").exists()


def test_expected_count_or_limits_cannot_perform_unapproved_partial_changes(
    tmp_path, db_path
):
    before = snapshot(db_path)
    with pytest.raises(ValueError, match="Eligible count changed"):
        tool.reconcile_database(
            db_path, **kwargs(tmp_path, db_path, expected_candidates=1)
        )
    with pytest.raises(ValueError, match="exceeds --max-updates"):
        tool.reconcile_database(db_path, **kwargs(tmp_path, db_path, max_updates=1))
    assert snapshot(db_path) == before
    assert not (tmp_path / "backup.sqlite3").exists()


def test_existing_backup_and_manifest_paths_cannot_be_overwritten(tmp_path, db_path):
    backup = tmp_path / "backup.sqlite3"
    backup.write_bytes(b"USER EXISTING FILE")
    with pytest.raises(FileExistsError):
        tool.reconcile_database(db_path, **kwargs(tmp_path, db_path))
    assert backup.read_bytes() == b"USER EXISTING FILE"
    assert tool.audit_database(db_path)["eligible_rows"] == 2

    backup.unlink()  # Test cleanup only; the operator script never deletes files.
    existing_manifest = tmp_path / "changes.jsonl"
    existing_manifest.write_text("preexisting")
    with pytest.raises(FileExistsError):
        tool.reconcile_database(db_path, **kwargs(tmp_path, db_path))
    assert not backup.exists()
    assert existing_manifest.read_text() == "preexisting"


def test_concurrent_source_edit_is_skipped_without_overwriting_it(
    tmp_path, db_path, monkeypatch
):
    backup = tool._backup_database

    def modify_after_backup(source, destination):
        backup(source, destination)
        with sqlite3.connect(source) as conn:
            conn.execute(
                "UPDATE messages SET text=? WHERE id=1",
                ("New concurrent manual legacy value",),
            )
            conn.commit()

    monkeypatch.setattr(tool, "_backup_database", modify_after_backup)
    result = tool.reconcile_database(db_path, **kwargs(tmp_path, db_path))
    assert result["applied"] == 1
    assert result["skipped_due_to_concurrent_changes"] == 1
    assert result["skipped_ids"] == [1]
    with sqlite3.connect(db_path) as conn:
        changed = conn.execute(
            "SELECT text,cleaned_text FROM messages WHERE id=1"
        ).fetchone()
        corrected = conn.execute(
            "SELECT text,cleaned_text FROM messages WHERE id=10"
        ).fetchone()
    assert changed == ("New concurrent manual legacy value", None)
    assert corrected == ("Clean me", "Clean me")


def test_trigger_error_rolls_back_whole_batch_and_leaves_verified_backup(
    tmp_path, db_path
):
    original = snapshot(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """CREATE TRIGGER reject_cleaned_second
               BEFORE UPDATE OF cleaned_text ON messages WHEN NEW.id=10
               BEGIN SELECT RAISE(ABORT,'simulated SQLite error'); END"""
        )
    with pytest.raises(sqlite3.DatabaseError, match="simulated SQLite error"):
        tool.reconcile_database(db_path, **kwargs(tmp_path, db_path))
    assert snapshot(db_path) == original
    assert (tmp_path / "backup.sqlite3").exists()
    events = [
        json.loads(line)
        for line in (tmp_path / "changes.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["prepared", "rolled_back"]


def test_default_cli_rejects_apply_without_explicit_backup_and_limits(
    db_path, tmp_path, capsys
):
    with pytest.raises(SystemExit) as exc:
        tool.main(["--database", str(db_path), "--apply"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        tool.main(["--database", str(db_path), "--backup", str(tmp_path / "x")])
    assert exc.value.code == 2
    assert not (tmp_path / "x").exists()
    assert tool.audit_database(db_path)["eligible_rows"] == 2


def test_nonexistent_sqlite_file_never_created(tmp_path):
    missing = tmp_path / "does-not-exist.sqlite3"
    with pytest.raises(FileNotFoundError):
        tool.audit_database(missing)
    assert not missing.exists()


def test_missing_columns_produce_failure_without_schema_migration(tmp_path):
    malformed = tmp_path / "old.sqlite3"
    with sqlite3.connect(malformed) as conn:
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT)")
    before = malformed.read_bytes()
    with pytest.raises(ValueError, match="Unsupported messages schema"):
        tool.audit_database(malformed)
    assert malformed.read_bytes() == before
