from contextlib import closing
import sqlite3

import pytest

import config
from storage import database, sqlite_policy as policy


@pytest.mark.parametrize("version,expected", [
    ((3, 40, 1), False), ((3, 44, 5), False), ((3, 44, 6), True),
    ((3, 45, 3), False), ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 51, 2), False), ((3, 51, 3), True), ((3, 52, 0), True),
    ((4, 0, 0), True),
])
def test_supported_version_boundaries(version, expected):
    assert policy.wal_supported(version) is expected


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "policy.db"
    monkeypatch.setattr(config, "DB_NAME", str(path))
    monkeypatch.setattr(config, "SQLITE_WAL_ENABLED", False)
    return path


def test_default_off_and_unsafe_opt_in(db_path, monkeypatch):
    assert database.initialize_journal_mode() == "delete"
    monkeypatch.setattr(config, "SQLITE_WAL_ENABLED", True)
    monkeypatch.setattr(policy, "wal_supported", lambda: False)
    assert database.initialize_journal_mode() == "delete"
    with closing(database.get_connection()) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_existing_wal_disabled_is_converted_without_losing_data(db_path, monkeypatch):
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE items(value)")
        conn.execute("INSERT INTO items VALUES (42)")
        conn.commit()
    monkeypatch.setattr(policy, "wal_supported", lambda: False)
    # Direct callers must not continue using an unsafe existing WAL.
    with pytest.raises(RuntimeError, match="Unsafe SQLite runtime"):
        database.get_connection()
    assert database.initialize_journal_mode() == "delete"
    with closing(database.get_connection()) as conn:
        assert conn.execute("SELECT value FROM items").fetchone()[0] == 42


def test_opt_in_wal_concurrent_reader_and_writer_and_restore(db_path, monkeypatch):
    # Exercise policy on CI runtimes that lack the upstream fix using an
    # isolated test DB with sequential writes and no simultaneous checkpoints.
    monkeypatch.setattr(policy, "wal_supported", lambda: True)
    monkeypatch.setattr(config, "SQLITE_WAL_ENABLED", True)
    assert database.initialize_journal_mode() == "wal"
    with closing(database.get_connection()) as writer:
        writer.execute("CREATE TABLE items(value)")
        writer.execute("INSERT INTO items VALUES (1)")
        writer.commit()
        with closing(database.get_connection()) as reader:
            assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert reader.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert reader.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
            assert reader.execute("PRAGMA journal_size_limit").fetchone()[0] == 16777216
            reader.execute("BEGIN")
            assert reader.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
            writer.execute("INSERT INTO items VALUES (2)")
            writer.commit()
            assert reader.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
            reader.commit()
            assert reader.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    monkeypatch.setattr(config, "SQLITE_WAL_ENABLED", False)
    assert database.initialize_journal_mode() == "delete"
    with closing(database.get_connection()) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT SUM(value) FROM items").fetchone()[0] == 3
