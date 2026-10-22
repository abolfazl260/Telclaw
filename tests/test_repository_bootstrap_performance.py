"""Regression coverage for once-per-database initialization and cache writes."""
import os

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")

import pytest

import config
from storage import database, data_normalizer
from storage import message_repository as repo_module
from storage.content_duplicate import content_hash
from storage.message_repository import MessageRepository
from scripts.rebuild_transfer_locations import rebuild_cache


@pytest.fixture
def perf_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "bootstrap.sqlite3"))
    return tmp_path / "bootstrap.sqlite3"


def _insert_raw_message(message_id, text="original raw source"):
    conn = database.get_connection()
    try:
        conn.execute(
            "INSERT INTO messages(channel_username,message_id,raw_text,text,date,"
            "sender_id,advertio_status) VALUES(?,?,?,?,?,?,?)",
            ("performance_test", message_id, text, text, "2026-10-22",
             message_id, "sent"),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM messages WHERE channel_username=? AND message_id=?",
            ("performance_test", message_id)
        ).fetchone()[0]
    finally:
        conn.close()


def _transfer(conn, mid, origin, dest, *, country="IR"):
    conn.execute(
        "INSERT INTO transferlist(processed_message_id,origin_country,"
        "origin_city,destination_country,destination_city,description) "
        "VALUES(?,?,?,?,?,?)",
        (mid, country, origin, country, dest, "cleaned description"),
    )


def test_repeated_repository_initialization_per_channel_is_schema_free(perf_db, monkeypatch):
    # A pre-existing message should get its legacy fingerprint on the first
    # startup, not on each subsequent 8-hour crawler cycle.
    database.initialize_db()
    old_id = _insert_raw_message(7001)
    mid = _insert_raw_message(7002)
    conn = database.get_connection()
    try:
        _transfer(conn, mid, "Tehran", "Mashhad")
        conn.commit()
    finally:
        conn.close()

    counts = {"db": 0, "normalizer": 0, "fingerprint": 0, "locations": 0}
    original_db = database.initialize_db
    original_alias = repo_module.initialize_normalization
    original_fingerprints = repo_module.ensure_duplicate_schema
    original_locations = MessageRepository._initialize_transfer_locations

    def track(name, fn):
        def wrapper(*args, **kwargs):
            counts[name] += 1
            return fn(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(database, "initialize_db", track("db", original_db))
    monkeypatch.setattr(repo_module, "initialize_normalization",
                        track("normalizer", original_alias))
    monkeypatch.setattr(repo_module, "ensure_duplicate_schema",
                        track("fingerprint", original_fingerprints))
    monkeypatch.setattr(MessageRepository, "_initialize_transfer_locations",
                        staticmethod(track("locations", original_locations)))

    repository = MessageRepository()
    for _ in range(6):
        repository.initialize()
    assert counts == {"db": 1, "normalizer": 1, "fingerprint": 1, "locations": 1}

    conn = database.get_connection()
    try:
        assert conn.execute("SELECT content_hash FROM messages WHERE id=?",
                            (old_id,)).fetchone()[0] == content_hash("original raw source")
        assert conn.execute("SELECT COUNT(*) FROM transfer_locations").fetchone()[0] == 0
    finally:
        conn.close()

    # The normal write path stores a new message fingerprint explicitly.
    # A later NULL on an unrelated legacy row should not force a full re-scan.
    next_id = _insert_raw_message(7003, "never backfilled in next channel")
    repository.initialize()
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT content_hash FROM messages WHERE id=?",
                            (next_id,)).fetchone()[0] is None
    finally:
        conn.close()


def test_location_cache_only_writes_on_new_or_modified_routes(perf_db):
    repository = MessageRepository()
    repository.initialize()
    mid = _insert_raw_message(7101)
    original = {
        "origin_city": "Tehran",
        "origin_country": "IR",
        "destination_city": "Toronto",
        "destination_country": "CA",
    }
    repository.save_category_record(mid, "transferlist", original)

    conn = database.get_connection()
    try:
        conn.execute("CREATE TABLE location_update_events(id INTEGER)")
        conn.execute("""CREATE TRIGGER detect_location_cache_update
            AFTER UPDATE ON transfer_locations
            BEGIN INSERT INTO location_update_events(id) VALUES(NEW.processed_message_id); END""")
        conn.commit()
    finally:
        conn.close()

    repository.save_category_record(mid, "transferlist", original)
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM location_update_events").fetchone()[0] == 0
    finally:
        conn.close()

    updated = {**original, "destination_city": "Vancouver"}
    repository.save_category_record(mid, "transferlist", updated)
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM location_update_events").fetchone()[0] == 1
        row = conn.execute("SELECT destination_city_canonical FROM transfer_locations "
                           "WHERE processed_message_id=?", (mid,)).fetchone()
        assert row[0] == "Vancouver"
    finally:
        conn.close()


def test_historical_location_rebuild_is_manual_preview_and_apply(perf_db):
    MessageRepository().initialize()
    old = _insert_raw_message(7201)
    conn = database.get_connection()
    try:
        _transfer(conn, old, "Tehran", "Mashhad")
        conn.commit()
    finally:
        conn.close()

    assert rebuild_cache() == {
        "scanned": 1, "missing": 1, "different": 0, "written": 0
    }
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transfer_locations").fetchone()[0] == 0
    finally:
        conn.close()

    assert rebuild_cache(apply=True) == {
        "scanned": 1, "missing": 1, "different": 0, "written": 1
    }
    assert rebuild_cache() == {
        "scanned": 1, "missing": 0, "different": 0, "written": 0
    }
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE transferlist SET origin_city='Shiraz' WHERE processed_message_id=?",
            (old,),
        )
        conn.commit()
    finally:
        conn.close()

    assert rebuild_cache() == {
        "scanned": 1, "missing": 0, "different": 1, "written": 0
    }
    assert rebuild_cache(apply=True)["written"] == 1
    conn = database.get_connection()
    try:
        assert conn.execute(
            "SELECT origin_city_canonical FROM transfer_locations "
            "WHERE processed_message_id=?", (old,),
        ).fetchone()[0] == "Shiraz"
        assert conn.execute("SELECT raw_text,advertio_status FROM messages "
                            "WHERE id=?", (old,)).fetchone()[:] == (
            "original raw source", "sent"
        )
        assert conn.execute("SELECT description FROM transferlist "
                            "WHERE processed_message_id=?", (old,)).fetchone()[0] == "cleaned description"
    finally:
        conn.close()


def test_normalization_uses_current_aliases_without_reseeding_on_each_ad(perf_db, monkeypatch):
    MessageRepository().initialize()
    data_normalizer.save_alias(
        "transferlist", "origin_city", "Once-only-alias", "Tehran",
        country_iso2="IR", admin_id=1485409432,
    )

    def fail_seeding(*args):
        raise AssertionError("normalization must not recreate defaults for each record")

    monkeypatch.setattr(data_normalizer, "_seed_defaults", fail_seeding)
    row = {"origin_country": "IR", "origin_city": "Once-only-alias"}
    for _ in range(3):
        normalized, _ = data_normalizer.normalize_category_data("transferlist", row)
        assert normalized["origin_city"] == "Tehran"

    # Simulate a Back Office edit committed by another process: the next
    # normalization loads the LIVE rule, even though bootstrap is cached.
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE normalization_aliases SET canonical_value='Mashhad' "
            "WHERE category='transferlist' AND field_name='origin_city' "
            "AND alias='Once-only-alias'",
        )
        conn.commit()
    finally:
        conn.close()
    updated, _ = data_normalizer.normalize_category_data("transferlist", row)
    assert updated["origin_city"] == "Mashhad"
