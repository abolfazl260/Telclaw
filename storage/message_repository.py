"""Persistence abstraction for the independent Telclaw pipeline queues."""

from storage import database


class MessageRepository:
    """Repository boundary used by collection, processing, and AI workers."""

    def initialize(self):
        database.initialize_db()

    def insert(self, **message):
        return database.insert_message(**message)

    def get_pending(self, limit=500, channel_username=None):
        return self.get_processing_pending(limit=limit, channel_username=channel_username)

    def get_processing_pending(self, limit=500, channel_username=None):
        return database.get_processing_pending_messages(
            limit=limit, channel_username=channel_username
        )

    def get_ai_pending(self, limit=100, channel_username=None):
        return database.get_ai_pending_messages(
            limit=limit, channel_username=channel_username
        )

    def get_previous_messages_by_sender(self, sender_id, before_id):
        return database.get_previous_messages_by_sender(sender_id, before_id)

    def update_message(self, message_id, channel_username, **fields):
        return database.update_message(message_id, channel_username, **fields)

    def mark_processed(self, message_id, channel_username, **fields):
        return self.update_message(message_id, channel_username, **fields)

    def mark_processing(self, message_id, channel_username):
        return self.update_message(
            message_id, channel_username, processing_status="processing"
        )

    def mark_processing_result(self, message_id, channel_username, *, success, **fields):
        if success:
            fields.update(processing_status="processed", ai_status="pending")
        else:
            fields.update(processing_status="failed")
        return self.update_message(message_id, channel_username, **fields)

    def mark_ai_processing(self, message_id, channel_username):
        return self.update_message(
            message_id, channel_username, ai_status="processing", ai_error=None
        )

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
        return database.save_category_record(processed_message_id, category, data)

    def set_target_date(self, channel_username, target_date):
        return database.set_channel_target_date(channel_username, target_date)

    def get_target_date(self, channel_username):
        return database.get_channel_target_date(channel_username)
