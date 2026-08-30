import importlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_database(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("GROQ_API_KEY", "key")
    monkeypatch.setenv("TELCLAW_GROQ_MODEL", "model")
    config = importlib.import_module("config")
    config.DB_NAME = str(tmp_path / "telclaw.db")
    database = importlib.import_module("storage.database")
    database.config.DB_NAME = config.DB_NAME
    return database, config.DB_NAME


def test_transferlist_schema_uses_air_cargo_fields(monkeypatch, tmp_path):
    database, db_name = _load_database(monkeypatch, tmp_path)

    database.initialize_db()

    with sqlite3.connect(db_name) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(transferlist)")}

    assert "vehicle_type" not in columns
    assert "brand" not in columns
    assert "model" not in columns
    assert "origin_city" in columns
    assert "destination_city" in columns
    assert "airline" in columns
    assert "departure_date" in columns
    assert "cargo_type" in columns


def test_transferlist_migration_removes_legacy_vehicle_columns(monkeypatch, tmp_path):
    database, db_name = _load_database(monkeypatch, tmp_path)

    with sqlite3.connect(db_name) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_username TEXT NOT NULL, message_id INTEGER NOT NULL, text TEXT, date TEXT NOT NULL, UNIQUE(channel_username,message_id))")
        conn.execute("CREATE TABLE transferlist (id INTEGER PRIMARY KEY AUTOINCREMENT, processed_message_id INTEGER NOT NULL UNIQUE, vehicle_type TEXT, brand TEXT, price REAL, currency TEXT, contact TEXT, features TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE)")
        conn.execute("INSERT INTO messages(channel_username,message_id,text,date) VALUES('chan',1,'cargo','2026-08-27')")
        conn.execute("INSERT INTO transferlist(processed_message_id,vehicle_type,brand,price,currency,contact,features) VALUES(1,'car','brand',10,'CAD','contact','[]')")

    database.initialize_db()

    with sqlite3.connect(db_name) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(transferlist)")}
        row = conn.execute("SELECT processed_message_id, price, currency, contact, features FROM transferlist").fetchone()

    assert "vehicle_type" not in columns
    assert "brand" not in columns
    assert "origin_city" in columns
    assert "destination_city" in columns
    assert "airline" in columns
    assert row == (1, 10, "CAD", "contact", "[]")
