"""Regression coverage for Telegram /database backup downloads."""

import sqlite3

import pytest

import config
from monitoring import telegram_monitor
from monitoring.telegram_monitor import ADMIN_USER_IDS, TelegramMonitor


def _monitor(monkeypatch):
    monitor = TelegramMonitor()
    monkeypatch.setattr(monitor, "_is_subscribed", lambda chat_id: True)
    return monitor


@pytest.mark.asyncio
async def test_small_database_is_sent_once_as_a_consistent_sqlite_file(tmp_path, monkeypatch):
    db_path = tmp_path / "small.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE records (value TEXT)")
        conn.execute("INSERT INTO records VALUES ('saved')")

    monkeypatch.setattr(config, "DB_NAME", str(db_path))
    monitor = _monitor(monkeypatch)
    documents = []
    notices = []

    async def fake_send_document(chat_id, file_path, caption):
        documents.append((chat_id, file_path.name, file_path.read_bytes(), file_path))
    async def fake_send(chat_id, text):
        notices.append(text)

    monkeypatch.setattr(monitor, "_send_document", fake_send_document)
    monkeypatch.setattr(monitor, "_send", fake_send)
    await monitor._download_database(min(ADMIN_USER_IDS))

    assert len(documents) == 1
    chat_id, name, payload, temporary_path = documents[0]
    assert chat_id == min(ADMIN_USER_IDS)
    assert name == db_path.name
    restored = tmp_path / "restored.db"
    restored.write_bytes(payload)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM records").fetchone()[0] == "saved"
    assert not temporary_path.exists()
    assert notices == []


@pytest.mark.asyncio
async def test_large_database_is_sent_in_ordered_40_mib_parts_with_wal_data(tmp_path, monkeypatch):
    db_path = tmp_path / "source.db"
    payload = bytes(range(256)) * 900
    # Leave committed data in the WAL while the backup runs.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records (value BLOB)")
        conn.execute("INSERT INTO records VALUES (?)", (payload,))
        conn.commit()

        monkeypatch.setattr(config, "DB_NAME", str(db_path))
        monkeypatch.setattr(telegram_monitor, "TELEGRAM_DATABASE_PART_BYTES", 64 * 1024)
        monitor = _monitor(monkeypatch)
        documents = []
        notices = []

        async def fake_send_document(chat_id, file_path, caption):
            documents.append((chat_id, file_path.name, file_path.read_bytes(), caption, file_path))
        async def fake_send(chat_id, text):
            notices.append(text)

        monkeypatch.setattr(monitor, "_send_document", fake_send_document)
        monkeypatch.setattr(monitor, "_send", fake_send)
        await monitor._download_database(min(ADMIN_USER_IDS))

    assert len(documents) >= 2
    assert all(0 < len(item[2]) <= 64 * 1024 for item in documents)
    assert [item[1] for item in documents] == [
        f"source.db.part{i:03d}" for i in range(1, len(documents) + 1)
    ]
    assert all(f"part {i}/{len(documents)}" in item[3]
               for i, item in enumerate(documents, 1))
    assert all(not item[4].exists() for item in documents)

    restored = tmp_path / "joined.db"
    restored.write_bytes(b"".join(item[2] for item in documents))
    with sqlite3.connect(restored) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM records").fetchone()[0] == payload
    assert len(notices) == 1
    assert "Join the .part files in numeric order" in notices[0]


@pytest.mark.asyncio
async def test_failed_part_stops_transfer_and_cleans_up_files(tmp_path, monkeypatch):
    db_path = tmp_path / "failure.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE records (value BLOB)")
        conn.execute("INSERT INTO records VALUES (zeroblob(200000))")

    monkeypatch.setattr(config, "DB_NAME", str(db_path))
    monkeypatch.setattr(telegram_monitor, "TELEGRAM_DATABASE_PART_BYTES", 64 * 1024)
    monitor = _monitor(monkeypatch)
    files = []
    notices = []

    async def fake_send_document(chat_id, file_path, caption):
        files.append(file_path)
        if len(files) == 2:
            raise RuntimeError("simulated Telegram upload failure")
    async def fake_send(chat_id, text):
        notices.append(text)

    monkeypatch.setattr(monitor, "_send_document", fake_send_document)
    monkeypatch.setattr(monitor, "_send", fake_send)
    await monitor._download_database(min(ADMIN_USER_IDS))

    assert len(files) == 2
    assert all(not path.exists() for path in files)
    assert len(notices) == 1
    assert "Failed to send database file" in notices[0]


@pytest.mark.asyncio
async def test_database_download_still_requires_subscription(tmp_path, monkeypatch):
    monitor = TelegramMonitor()
    messages = []
    monkeypatch.setattr(monitor, "_is_subscribed", lambda chat_id: False)

    async def fake_send(chat_id, text):
        messages.append(text)

    async def forbidden_document(chat_id, file_path, caption):
        raise AssertionError("No database file may be sent without a subscription")

    monkeypatch.setattr(monitor, "_send", fake_send)
    monkeypatch.setattr(monitor, "_send_document", forbidden_document)
    await monitor._download_database(min(ADMIN_USER_IDS))

    assert len(messages) == 1
    assert "not subscribed" in messages[0]
