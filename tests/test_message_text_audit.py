"""Read-only and privacy regression tests for the optional text-column audit."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from scripts import audit_message_text_columns as audit


@pytest.fixture
def audit_db(tmp_path):
    path = tmp_path / "audit fixture.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE messages(
                id INTEGER PRIMARY KEY,
                processing_status TEXT,
                raw_text TEXT,
                text TEXT,
                cleaned_text TEXT
            )"""
        )
        conn.executemany(
            "INSERT INTO messages(processing_status,raw_text,text,cleaned_text) VALUES(?,?,?,?)",
            [
                ("pending", "SECRET 555-0000   source", "SECRET 555-0000   source", None),
                ("pending", None, None, None),
                ("processing", "", "", ""),
                ("processed", "A   B", "A B", "A B"),
                ("processed", "C", "manually edited", "C"),
                ("processed", "D", None, None),
                ("processed", "E", None, "E"),
                ("failed", None, "legacy-only", ""),
                (None, "F", "changed before processing", None),
            ],
        )
    return path


def test_audit_aggregates_null_empty_and_divergent_values(audit_db):
    report = audit.audit_database(audit_db)
    by_status = {
        group["processing_status"]: group
        for group in report["by_processing_status"]
    }
    assert report["summary"]["total_rows"] == 9
    assert set(by_status) == {"pending", "processing", "processed", "failed", None}
    assert by_status["pending"]["total_rows"] == 2
    assert by_status["pending"]["cleaned_text_null"] == 2
    assert by_status["pending"]["text_null"] == 1
    assert by_status["processing"]["raw_text_empty"] == 1
    assert by_status["processing"]["text_empty"] == 1
    assert by_status["processed"]["total_rows"] == 4
    assert by_status["processed"]["processed_text_cleaned_different"] == 1
    assert by_status["processed"]["processed_text_cleaned_one_null"] == 1
    assert by_status["processed"]["cleaned_text_null"] == 1
    assert by_status["failed"]["raw_text_null"] == 1
    assert by_status["failed"]["cleaned_text_empty"] == 1
    assert by_status[None]["unprocessed_text_raw_different"] == 1
    assert report["summary"]["processed_text_cleaned_different"] == 1


def test_audit_uses_select_only_readonly_uri_and_preserves_database(audit_db, monkeypatch):
    before = hashlib.sha256(audit_db.read_bytes()).hexdigest()
    statements = []
    opened = []
    original_connect = sqlite3.connect

    def connect_spy(database, **kwargs):
        opened.append((database, kwargs))
        connection = original_connect(database, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(audit.sqlite3, "connect", connect_spy)
    audit.audit_database(audit_db)
    after = hashlib.sha256(audit_db.read_bytes()).hexdigest()

    assert before == after
    assert len(opened) == 1
    assert opened[0][1]["uri"] is True
    assert opened[0][0].startswith("file:")
    assert opened[0][0].endswith("?mode=ro")
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")


def test_cli_reports_only_aggregates_not_message_contents(audit_db, capsys):
    assert audit.main(["--database", str(audit_db)]) == 0
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["summary"]["total_rows"] == 9
    for secret in ("SECRET", "555-0000", "legacy-only", "manually edited"):
        assert secret not in output
    assert str(audit_db) not in output


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        audit.audit_database(missing)
    assert not missing.exists()


def test_empty_database_and_missing_schema(tmp_path):
    path = tmp_path / "empty.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE messages(processing_status TEXT, raw_text TEXT, text TEXT, cleaned_text TEXT)"
        )
    assert audit.audit_database(path) == {
        "summary": {name: 0 for name in audit._METRICS},
        "by_processing_status": [],
    }
    wrong_schema = tmp_path / "wrong.sqlite3"
    with sqlite3.connect(wrong_schema) as conn:
        conn.execute("CREATE TABLE something_else(id INTEGER)")
    with pytest.raises(sqlite3.OperationalError):
        audit.audit_database(wrong_schema)
