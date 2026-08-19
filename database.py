"""Backward-compatible facade for the SQLite storage layer.

New code should import from ``storage.database`` directly.
"""

from storage.database import (
    get_connection,
    get_channel_target_date,
    initialize_db,
    insert_message,
    set_channel_target_date,
)

__all__ = [
    "get_connection",
    "get_channel_target_date",
    "initialize_db",
    "insert_message",
    "set_channel_target_date",
]
