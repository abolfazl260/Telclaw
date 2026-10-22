"""Manually preview or rebuild historical transfer location cache.

Do not run during a live high-volume crawl/publisher cycle: rebuilding reads
all transfer records and briefly writes batches of cache rows. Back up SQLite.
Unlike normal crawler startup this intentionally scans historical transferlist.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import database
from storage.location_normalizer import normalize_location
from storage.message_repository import MessageRepository


def rebuild_cache(*, apply=False, commit_every=250):
    """Compare all historical routes; write only missing or changed cache rows."""
    if commit_every < 1:
        raise ValueError("commit_every must be positive")
    if apply:
        # Schema creation is a setup operation, never part of read-only preview.
        MessageRepository._initialize_transfer_locations()
    conn = database.get_connection()
    summary = {"scanned": 0, "missing": 0, "different": 0, "written": 0}
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='transfer_locations'"
        ).fetchone()
        if not exists:
            raise RuntimeError("transfer_locations is missing; start Telclaw first")
        rows = conn.execute("""
            SELECT t.processed_message_id,
                   t.origin_city,t.origin_country,
                   t.destination_city,t.destination_country,
                   c.id AS cache_id,
                   c.origin_city_canonical AS old_origin_city,
                   c.origin_city_key AS old_origin_key,
                   c.origin_country_iso2 AS old_origin_country,
                   c.destination_city_canonical AS old_destination_city,
                   c.destination_city_key AS old_destination_key,
                   c.destination_country_iso2 AS old_destination_country
            FROM transferlist t
            LEFT JOIN transfer_locations c ON c.processed_message_id=t.processed_message_id
            ORDER BY t.id
        """)
        for row in rows:
            data = dict(row)
            summary["scanned"] += 1
            origin = normalize_location(data["origin_city"], data["origin_country"])
            destination = normalize_location(data["destination_city"], data["destination_country"])
            new_values = (
                origin["city"], origin["city_key"], origin["country_iso2"],
                destination["city"], destination["city_key"], destination["country_iso2"],
            )
            old_values = (
                data["old_origin_city"], data["old_origin_key"], data["old_origin_country"],
                data["old_destination_city"], data["old_destination_key"], data["old_destination_country"],
            )
            if data["cache_id"] is not None and new_values == old_values:
                continue
            if data["cache_id"] is None:
                summary["missing"] += 1
            else:
                summary["different"] += 1
            if apply:
                MessageRepository._save_transfer_location_conn(conn, data)
                summary["written"] += 1
                if summary["written"] % commit_every == 0:
                    conn.commit()
        if apply:
            conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write missing/stale cache rows; BACK UP SQLite first")
    args = parser.parse_args()
    outcome = rebuild_cache(apply=args.apply)
    print(("APPLIED" if args.apply else "PREVIEW"), outcome)


if __name__ == "__main__":
    main()
