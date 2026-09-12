import sqlite3

from storage.content_duplicate import (
    content_hash,
    ensure_duplicate_schema,
    find_duplicate,
    normalize_message_text,
    store_content_hash,
)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            text TEXT,
            sender_id INTEGER,
            UNIQUE(channel_username, message_id)
        )"""
    )
    ensure_duplicate_schema(conn)
    return conn


def _add(conn, channel, message_id, sender_id, text):
    conn.execute(
        "INSERT INTO messages(channel_username,message_id,sender_id,text) VALUES(?,?,?,?)",
        (channel, message_id, sender_id, text),
    )
    row_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    store_content_hash(conn, row_id, text)
    return row_id


def test_same_user_same_message_different_groups_is_duplicate():
    conn = _db()
    _add(conn, "group_a", 101, 42, "Apartment for rent in Toronto with two bedrooms")
    duplicate = find_duplicate(conn, 42, "Apartment for rent in Toronto with two bedrooms")
    assert duplicate["message_id"] == 101


def test_same_user_different_messages_are_accepted():
    conn = _db()
    _add(conn, "group_a", 101, 42, "Apartment for rent in Toronto with two bedrooms")
    assert find_duplicate(conn, 42, "House for sale in Toronto with three bedrooms") is None


def test_different_users_same_message_are_accepted():
    conn = _db()
    _add(conn, "group_a", 101, 42, "Apartment for rent in Toronto with two bedrooms")
    assert find_duplicate(conn, 99, "Apartment for rent in Toronto with two bedrooms") is None


def test_harmless_formatting_and_case_differences_are_duplicate():
    conn = _db()
    original = "  Apartment FOR rent in Toronto\nwith two   bedrooms  "
    _add(conn, "group_a", 101, 42, original)
    assert normalize_message_text(original) == normalize_message_text("apartment for RENT in Toronto with two bedrooms")
    assert find_duplicate(conn, 42, "apartment for RENT in Toronto\twith two bedrooms") is not None


def test_different_telegram_message_ids_do_not_prevent_duplicate_detection():
    conn = _db()
    _add(conn, "group_a", 101, 42, "Apartment for rent in Toronto with two bedrooms")
    duplicate = find_duplicate(conn, 42, "Apartment for rent in Toronto with two bedrooms")
    assert duplicate["message_id"] != 102
    assert content_hash("Apartment for rent in Toronto with two bedrooms") is not None


def test_media_only_message_without_text_is_not_duplicate():
    conn = _db()
    _add(conn, "group_a", 101, 42, None)
    assert find_duplicate(conn, 42, None) is None


def test_sender_and_hash_index_exists():
    conn = _db()
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(messages)").fetchall()}
    assert "idx_messages_sender_content_hash" in indexes
