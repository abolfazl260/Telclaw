"""Persistence layer."""

from . import database as database

# Keep the declared transferlist schema compatible with publisher fields before
# database.initialize_db() performs its normal migration/rebuild logic.
database.CATEGORY_TABLES["transferlist"].setdefault("transfer_role", "TEXT")
