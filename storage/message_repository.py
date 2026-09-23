"""Persistence abstraction for the independent Telclaw pipeline queues."""

from storage import database
from storage.location_normalizer import normalize_location


class MessageRepository:
    """Repository boundary used by collection, processing, and AI workers."""

    def initialize(self):
        database.initialize_db()
        self._initialize_transfer_locations()

    @staticmethod
    def _initialize_transfer_locations():
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
            rows = conn.execute("""SELECT t.processed_message_id, t.origin_city, t.origin_country,
                                      t.destination_city, t.destination_country
                                 FROM transferlist t""").fetchall()
            for row in rows:
                MessageRepository._save_transfer_location_conn(conn, dict(row))
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
            destination_country_iso2=excluded.destination_country_iso2""", (
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

    def get_ai_pending(self, limit=100, channel_username=None):
        return database.get_ai_pending_messages(limit=limit, channel_username=channel_username)

    def get_advertio_pending(self, limit=100, channel_username=None):
        return database.get_advertio_pending_messages(limit=limit, channel_username=channel_username)

    def get_latest_message_id(self, channel_username):
        return database.get_latest_message_id(channel_username)

    def get_previous_messages_by_sender(self, sender_id, before_id):
        return database.get_previous_messages_by_sender(sender_id, before_id)

    def update_message(self, message_id, channel_username, **fields):
        return database.update_message(message_id, channel_username, **fields)

    def mark_processed(self, message_id, channel_username, **fields):
        return self.update_message(message_id, channel_username, **fields)

    def mark_processing(self, message_id, channel_username):
        return self.update_message(message_id, channel_username, processing_status="processing")

    def mark_processing_result(self, message_id, channel_username, *, success, **fields):
        if success:
            fields.update(processing_status="processed", classification_status="pending", ai_status="waiting")
        else:
            fields.update(processing_status="failed")
        return self.update_message(message_id, channel_username, **fields)

    def mark_classification_processing(self, message_id, channel_username):
        return self.update_message(message_id, channel_username, classification_status="processing", classification_error=None)

    def mark_classification_result(self, message_id, channel_username, *, category=None, success=True, error=None, processed_at=None, attempts=None):
        fields = {"classification_processed_at": processed_at}
        if attempts is not None:
            fields["classification_attempts"] = attempts
        if success:
            fields.update(classification_status="processed", classification_category=category, classification_error=None, ai_category=category if category != "none" else None, ai_status="pending" if category != "none" else "skipped")
        else:
            fields.update(classification_status="failed", classification_error=error, ai_status="waiting")
        return self.update_message(message_id, channel_username, **fields)

    def mark_ai_processing(self, message_id, channel_username):
        return self.update_message(message_id, channel_username, ai_status="processing", ai_error=None)

    def mark_ai_result(self, message_id, channel_username, *, success, **fields):
        if success:
            fields.update(ai_status="processed")
        else:
            fields.pop("ai_error", None)
            fields.update(ai_status="failed", ai_error=None)
        return self.update_message(message_id, channel_username, **fields)

    def mark_ai_skipped(self, message_id, channel_username, *, reason, **fields):
        fields.update(ai_status="skipped", ai_error=f"skipped:{reason}")
        return self.update_message(message_id, channel_username, **fields)

    def save_category_record(self, processed_message_id, category, data):
        result = database.save_category_record(processed_message_id, category, data)
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
        """Clear media_path for exactly one delivered message record."""
        conn = database.get_connection()
        try:
            cursor = conn.execute(
                "UPDATE messages SET media_path=NULL WHERE channel_username=? AND message_id=?",
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
