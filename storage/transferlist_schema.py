"""Schema compatibility for transfer-list publishing fields."""

from storage import database


def ensure_transferlist_schema():
    """Register transfer_role before the normal DB migration runs.

    The database layer rebuilds transferlist when its declared schema changes.
    Registering the field here lets that existing migration preserve all current
    rows while adding transfer_role to older SQLite databases.
    """
    database.CATEGORY_TABLES["transferlist"].setdefault("transfer_role", "TEXT")
    database.initialize_db()
