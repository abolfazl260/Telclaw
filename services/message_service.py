"""Application service for message persistence use-cases."""

from storage.message_repository import MessageRepository


class MessageService:
    """Own the application-facing message persistence contract."""

    def __init__(self, repository=None):
        self.repository = repository or MessageRepository()

    def save_collected_message(self, **message):
        return self.repository.insert(**message)

    def initialize(self):
        self.repository.initialize()
