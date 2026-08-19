"""Persistence abstraction for collected, processed, and AI-enriched messages."""

from storage import database


class MessageRepository:
    """Repository boundary used by application services."""

    def initialize(self):
        database.initialize_db()

    def insert(self, **message):
        return database.insert_message(**message)

    def get_pending(self, limit=500, channel_username=None):
        return database.get_messages_by_status(
            "collected", limit=limit, channel_username=channel_username
        )

    def get_ai_pending(self, limit=100, channel_username=None):
        return database.get_ai_pending_messages(
            limit=limit, channel_username=channel_username
        )

    def mark_processed(self, message_id, channel_username, **fields):
        return database.update_processed_message(
            message_id, channel_username, **fields
        )

    def save_category_record(self, processed_message_id, category, data):
        return database.save_category_record(processed_message_id, category, data)

    def set_target_date(self, channel_username, target_date):
        return database.set_channel_target_date(channel_username, target_date)

    def get_target_date(self, channel_username):
        return database.get_channel_target_date(channel_username)
