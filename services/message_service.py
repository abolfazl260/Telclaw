"""Application service for message persistence boundaries."""

from storage import database


class MessageService:
    """Owns the application-facing message persistence contract."""

    def save_collected_message(self, **message):
        return database.insert_message(**message)

    def initialize(self):
        database.initialize_db()
