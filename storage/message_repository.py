"""Persistence abstraction for the independent Telclaw pipeline queues."""

from storage import database
import config
from pathlib import Path
from threading import Lock
from storage.content_duplicate import ensure_duplicate_schema
from storage.location_normalizer import normalize_location
from storage.data_normalizer import initialize as initialize_normalization, normalize_category_data


_BOOTSTRAP_LOCK = Lock()
_READY_DATABASE = None


def _database_identity():
    """Identify the active SQLite file, including recreation at the same path."""
    path = Path(config.DB_NAME).resolve()
    try:
        stat = path.stat()
        return (str(path), stat.st_dev, stat.st_ino)
    except FileNotFoundError:
        return (str(path), None, None)


class MessageRepository:
    """Repository boundary used by collection, processing, and AI workers."""

    def initialize(self):
        """Initialize this database once per process, not once per channel.

        New batches continue querying the latest normalization aliases; this
        guard only skips schema setup, default seeding and legacy hash backfill.
        """
        global _READY_DATABASE
        identity = _database_identity()
        if identity == _READY_DATABASE and identity[1] is not None:
            return
        with _BOOTSTRAP_LOCK:
            identity = _database_identity()
            if identity == _READY_DATABASE and identity[1] is not None:
                return
            database.initialize_db()
            initialize_normalization()
            conn = database.get_connection()
            try:
                # Existing content fingerprints are recovered once per process,
                # not repeatedly for each of the crawler's channels.
                ensure_duplicate_schema(conn)
            finally:
                conn.close()
            self._initialize_transfer_locations()
            _READY_DATABASE = _database_identity()

    @staticmethod
    def _initialize_transfer_locations():
        """Create the cache schema; NEVER rewrite historical transfer rows."""
        conn = database.get_connection()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS transfer_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                processed_message_id INTEGER NOT NULL UNIQUE,
                origin_city_canonical TEXT,
                origin_city_key TEXT,
                origin_country_iso2 TEXT,
                destination_city_canonical TEXT,
                destination_city_key TEXT,
                destination_country_iso2 TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transfer_locations_origin ON transfer_locations(origin_country_iso2, origin_city_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transfer_locations_destination ON transfer_locations(destination_country_iso2, destination_city_key)")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _save_transfer_location_conn(conn, data):
        origin = normalize_location(data.get("origin_city"), data.get("origin_country"))
        destination = normalize_location(data.get("destination_city"), data.get("destination_country"))
        conn.execute("""INSERT INTO transfer_locations(
            processed_message_id,
            origin_city_canonical, origin_city_key, origin_country_iso2,
            destination_city_canonical, destination_city_key, destination_country_iso2
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(processed_message_id) DO UPDATE SET
            origin_city_canonical=excluded.origin_city_canonical,
            origin_city_key=excluded.origin_city_key,
            origin_country_iso2=excluded.origin_country_iso2,
            destination_city_canonical=excluded.destination_city_canonical,
            destination_city_key=excluded.destination_city_key,
            destination_country_iso2=excluded.destination_country_iso2
        WHERE transfer_locations.origin_city_canonical IS NOT excluded.origin_city_canonical
           OR transfer_locations.origin_city_key IS NOT excluded.origin_city_key
           OR transfer_locations.origin_country_iso2 IS NOT excluded.origin_country_iso2
           OR transfer_locations.destination_city_canonical IS NOT excluded.destination_city_canonical
           OR transfer_locations.destination_city_key IS NOT excluded.destination_city_key
           OR transfer_locations.destination_country_iso2 IS NOT excluded.destination_country_iso2""", (
            data.get("processed_message_id"),
            origin["city"], origin["city_key"], origin["country_iso2"],
            destination["city"], destination["city_key"], destination["country_iso2"],
        ))

    def insert(self, **message):
        return database.insert_message(**message)

    def get_pending(self, limit=500, channel_username=None):
        return self.get_processing_pending(limit=limit, channel_username=channel_username)

    def get_processing_pending(self, limit=500, channel_username=None):
        return database.get_processing_pending_messages(limit=limit, channel_username=channel_username)

    def get_classification_pending(self, limit=50, channel_username=None):
        return database.get_classification_pending_messages(limit=limit, channel_username=channel_username)

    def get_classification_queue_status(self):
        return database.get_classification_queue_status()

    def retry_failed_classifications(self):
        return database.retry_failed_classifications()

    def retry_invalid_provider_output_classifications(self):
        return database.retry_invalid_provider_output_classifications()

    def retry_failed_extractions(self, invalid_output_only=False):
        return database.retry_failed_extractions(invalid_output_only=invalid_output_only)

    def requeue_disabled_jobs_extractions(self, limit=100):
        return database.requeue_disabled_jobs_extractions(limit=limit)

    def requeue_missing_jobs_extractions(self, limit=100):
        return database.requeue_missing_jobs_extractions(limit=limit)

    def get_ai_pending(self, limit=100, channel_username=None):
        return database.get_ai_pending_messages(limit=limit, channel_username=channel_username)

    def get_advertio_pending(self, limit=100, channel_username=None, before_datetime=None):
        return database.get_advertio_pending_messages(
            limit=limit,
            channel_username=channel_username,
            before_datetime=before_datetime,
        )

    def get_latest_message_id(self, channel_username):
        return database.get_latest_message_id(channel_username)

    def get_previous_messages_by_sender(self, sender_id, before_id):
        return database.get_previous_messages_by_sender(sender_id, before_id)

    def update_message(self, message_id, channel_username, **fields):
        return database.update_message(message_id, channel_username, **fields)

    def mark_processed(self, message_id, channel_username, **fields):
        return self.update_message(message_id, channel_username, **fields)

    def mark_processing(self, message_id, channel_username):
        return database.claim_processing_message(message_id, channel_username)

    def mark_processing_result(self, message_id, channel_username, *, success, **fields):
        if success:
            fields.update(processing_status="processed", classification_status="pending", ai_status="waiting")
        else:
            fields.update(processing_status="failed")
        fields.setdefault("processing_started_at", None)
        return self.update_message(message_id, channel_username, **fields)

    def mark_classification_processing(self, message_id, channel_username):
        return database.claim_classification_message(message_id, channel_username, config.AI_CLASSIFICATION_MAX_RETRIES)

    def mark_classification_result(self, message_id, channel_username, *, category=None, success=True, error=None, processed_at=None, attempts=None):
        fields = {"classification_processed_at": processed_at, "classification_started_at": None}
        if attempts is not None:
            fields["classification_attempts"] = attempts
        if success:
            fields.update(classification_status="processed", classification_category=category, classification_error=None, ai_category=category if category != "none" else None, ai_status="pending" if category != "none" else "skipped")
        else:
            fields.update(classification_status="failed", classification_error=error, ai_status="waiting")
        return self.update_message(message_id, channel_username, **fields)

    def mark_ai_processing(self, message_id, channel_username):
        return database.claim_ai_message(message_id, channel_username)

    def mark_ai_result(self, message_id, channel_username, *, success, **fields):
        # Only fixed, non-sensitive error codes are allowed into persistent data.
        # Provider error messages, request IDs and raw responses stay in logs.
        reason = fields.pop("ai_error", None)
        if success:
            fields.update(ai_status="processed", ai_error=None, ai_started_at=None)
        else:
            safe_reason = reason if reason in {"invalid_provider_output", "rate_limit"} else None
            fields.update(ai_status="failed", ai_error=safe_reason, ai_started_at=None)
        return self.update_message(message_id, channel_username, **fields)

    def mark_ai_skipped(self, message_id, channel_username, *, reason, **fields):
        fields.update(ai_status="skipped", ai_error=f"skipped:{reason}", ai_started_at=None)
        return self.update_message(message_id, channel_username, **fields)

    def save_category_record(self, processed_message_id, category, data):
        normalized_data, _ = normalize_category_data(category, data)
        result = database.save_category_record(processed_message_id, category, normalized_data)
        if category == "transferlist":
            conn = database.get_connection()
            try:
                row = conn.execute(
                    "SELECT processed_message_id, origin_city, origin_country, destination_city, destination_country FROM transferlist WHERE processed_message_id=?",
                    (processed_message_id,),
                ).fetchone()
                if row:
                    self._save_transfer_location_conn(conn, dict(row))
                    conn.commit()
            finally:
                conn.close()
        return result

    def get_category_record(self, processed_message_id, category):
        return database.get_category_record(processed_message_id, category)

    def mark_advertio_result(self, message_id, channel_username, *, status, lead_id=None, error=None, processed_at=None):
        return self.update_message(
            message_id,
            channel_username,
            advertio_status=status,
            advertio_lead_id=lead_id,
            advertio_error=error,
            advertio_processed_at=processed_at,
        )

    def clear_media_path(self, message_id, channel_username):
        """Clear legacy and ordered local media paths for one delivered record."""
        conn = database.get_connection()
        try:
            cursor = conn.execute(
                "UPDATE messages SET media_path=NULL, media_paths=NULL WHERE channel_username=? AND message_id=?",
                (channel_username, message_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def set_target_date(self, channel_username, target_date):
        return database.set_channel_target_date(channel_username, target_date)

    def get_target_date(self, channel_username):
        return database.get_channel_target_date(channel_username)
