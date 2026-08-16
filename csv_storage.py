import csv
import os

CSV_FILE = "messages.csv"
ID_FILE = "messageid.csv"

HEADERS = [
    "message_id",
    "unique_message_key",
    "date",
    "channel_id",
    "channel_username",
    "channel_name",
    "sender_id",
    "sender_username",
    "sender_type",
    "text",
    "has_media",
    "media_type",
    "file_unique_id"
]

ID_HEADERS = [
    "unique_key"
]


# -----------------------
# init files
# -----------------------
def _ensure_file(path, headers):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)


def _load_existing_ids():
    if not os.path.exists(ID_FILE):
        return set()

    with open(ID_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        return {row[0] for row in reader if row}


def _append_id(unique_key):
    with open(ID_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([unique_key])


# -----------------------
# main save
# -----------------------
def save_message(
    message_id,
    channel_username,
    text,
    date,

    channel_id=None,
    channel_name=None,

    sender_id=None,
    sender_username=None,
    sender_type=None,

    has_media=False,
    media_type=None,
    file_unique_id=None,

    unique_key=None
):
    _ensure_file(CSV_FILE, HEADERS)
    _ensure_file(ID_FILE, ID_HEADERS)

    if unique_key is None:
        unique_key = f"{channel_id}_{message_id}"

    # load existing ids
    existing_ids = _load_existing_ids()

    # -----------------------
    # DUPLICATE CHECK
    # -----------------------
    if unique_key in existing_ids:
        return False  # skipped

    # -----------------------
    # SAVE MAIN CSV
    # -----------------------
    row = [
        message_id,
        unique_key,
        date,
        channel_id,
        channel_username,
        channel_name,
        sender_id,
        sender_username,
        sender_type,
        text,
        has_media,
        media_type,
        file_unique_id
    ]

    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)

    # -----------------------
    # SAVE ID INDEX
    # -----------------------
    _append_id(unique_key)

    return True