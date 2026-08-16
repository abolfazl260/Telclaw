"""Import legacy crawler CSV data into SQLite.

The import is idempotent: existing SQLite rows are ignored by the
(channel_username, message_id) unique constraint.
"""

import csv
from pathlib import Path

import database


CSV_FILE = Path("messages.csv")


def _as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def migrate(csv_path=CSV_FILE):
    path = Path(csv_path)
    if not path.exists():
        print(f"No CSV file found: {path}")
        return 0

    database.initialize_db()
    imported = 0

    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"message_id", "channel_username", "text", "date"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "CSV is missing required columns: " + ", ".join(sorted(missing))
            )

        for row in reader:
            channel_username = (row.get("channel_username") or "").strip()
            if not channel_username:
                continue

            try:
                message_id = int(row["message_id"])
            except (TypeError, ValueError):
                continue

            inserted = database.insert_message(
                channel_username=channel_username,
                message_id=message_id,
                text=row.get("text") or "",
                date_str=row.get("date") or "",
                channel_id=row.get("channel_id") or None,
                channel_name=row.get("channel_name") or None,
                sender_id=row.get("sender_id") or None,
                sender_username=row.get("sender_username") or None,
                sender_type=row.get("sender_type") or None,
                has_media=_as_bool(row.get("has_media")),
                media_type=row.get("media_type") or None,
                file_unique_id=row.get("file_unique_id") or None,
            )
            imported += int(inserted)

    return imported


if __name__ == "__main__":
    count = migrate()
    print(f"Imported {count} new message(s) into SQLite.")
