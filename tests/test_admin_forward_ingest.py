"""Administrator-forwarded message ingestion and provenance regressions."""
from datetime import datetime, timedelta, timezone

import pytest

import config
from storage import database
from monitoring.admin_forward_ingest import (
    ADMIN_FORWARD_PREFIX, SENDER_TYPE, extract_text_username,
    ingest_admin_forward, is_forwarded, word_count,
)
from monitoring.telegram_monitor import TelegramMonitor


@pytest.fixture
def inbox_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "admin-forward.sqlite"))
    database.initialize_db()
    return config.DB_NAME


def forwarded(text, *, admin_id=1485409432, mid=1, age_days=1, caption=False):
    stamp = int((datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp())
    return {
        "message_id": mid,
        "from": {"id": admin_id},
        "chat": {"id": admin_id, "type": "private"},
        "forward_origin": {"type": "hidden_user", "date": stamp},
        ("caption" if caption else "text"): text,
    }


def long_post(handle="@Job_Posting_User"):
    return (
        "سلام امروز برای شرکت معتبر در شهر تورنتو به دنبال نیرو هستیم "
        "همه جزئیات شرایط سابقه و میزان حقوق در متن درج شده است "
        f"Username: {handle}"
    )


def read_rows():
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id")]


def test_text_handle_selection():
    assert extract_text_username(long_post("@Job_Owner")) == "@Job_Owner"
    assert extract_text_username("Contact: @Main_User and follow @Promo_Channel") == "@Main_User"
    assert extract_text_username("username: @First_Person username: @Other_Person") is None
    assert extract_text_username("See @First_Person and @Second_Person") is None
    assert extract_text_username("See https://t.me/Job_Owner") == "@Job_Owner"
    assert extract_text_username("Source https://t.me/someChannel/333") is None
    assert extract_text_username("No Telegram username or contact in this text") is None
    assert extract_text_username("email contact someone@sample.com") is None


def test_word_count_strictly_more_than_ten():
    ten = "one two three four five six seven eight nine @job_owner"
    assert word_count(ten) == 10
    assert word_count(ten + " hello") == 11


def test_accepted_forward_is_pending_with_original_date_and_text_contact(inbox_db):
    text = long_post()
    message = forwarded(text, mid=42, age_days=3)
    status, value = ingest_admin_forward(message)
    assert status == "saved" and value == "@Job_Posting_User"
    rows = read_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["channel_username"] == f"{ADMIN_FORWARD_PREFIX}1485409432"
    assert row["message_id"] == 42
    assert row["sender_username"] == "Job_Posting_User"
    assert row["sender_id"] is None
    assert row["sender_type"] == SENDER_TYPE
    assert row["sender_is_original"] == 0
    assert row["message_link"] is None
    assert row["raw_text"] == text and row["text"] == text
    assert row["cleaned_text"] is None
    assert row["processing_status"] == "pending"
    assert row["classification_status"] == "waiting"
    assert row["ai_status"] == "waiting"
    date = datetime.fromisoformat(row["date"])
    assert timedelta(days=2, hours=23) < datetime.now(timezone.utc) - date < timedelta(days=3, hours=1)


def test_forwarded_caption_and_idempotent_duplicate(inbox_db):
    text = long_post("@Other_Poster")
    assert ingest_admin_forward(forwarded(text, mid=1, caption=True))[0] == "saved"
    assert ingest_admin_forward(forwarded(text, mid=1, caption=True))[0] == "duplicate"
    assert ingest_admin_forward(forwarded(text, mid=2, admin_id=266809220))[0] == "duplicate"
    assert len(read_rows()) == 1


@pytest.mark.parametrize("message, expected", [
    ({"message_id": 5, "from": {"id": 1485409432},
      "text": long_post()}, "not_forwarded"),
    (forwarded("one two three four five six seven eight nine @job_owner"), "too_short"),
    (forwarded(long_post(handle="@aaa") + " @Second_User"), "missing_username"),
    (forwarded("این پیام فوروارد بیشتر از ده کلمه دارد اما هیچ آیدی تلگرامی ندارد و فقط نمونه است"), "missing_username"),
    ({**forwarded(long_post()), "forward_origin": {"type": "hidden_user"}}, "missing_forward_date"),
])
def test_ineligible_forward_not_inserted(inbox_db, message, expected):
    assert ingest_admin_forward(message)[0] == expected
    assert read_rows() == []


def test_only_actual_forward_metadata_is_accepted():
    assert is_forwarded(forwarded(long_post()))
    assert not is_forwarded({"text": "Forwarded from @Job_Owner " + long_post()})
    assert is_forwarded({"forward_from_chat": {"id": -100123}, "forward_date": 123})


@pytest.mark.asyncio
async def test_private_admin_bot_accepts_forward_and_rejects_other_chats(inbox_db, monkeypatch):
    monitor = TelegramMonitor()
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(monitor, "_send", fake_send)
    forwarded_text = long_post("@Real_Owner")
    admin_update = {"message": forwarded(forwarded_text, mid=51)}
    await monitor._handle_update(admin_update)
    assert len(read_rows()) == 1
    assert "@Real_Owner" in sent[-1][1]
    assert "Queued for normal processing" in sent[-1][1]

    unauthorized = {"message": forwarded(long_post("@Another_User"), admin_id=999999, mid=52)}
    await monitor._handle_update(unauthorized)
    group_message = forwarded(long_post("@Another_User"), mid=53)
    group_message["chat"] = {"id": -100123, "type": "supergroup"}
    await monitor._handle_update({"message": group_message})
    assert len(read_rows()) == 1
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_admin_plain_text_and_commands_are_not_imported(inbox_db, monkeypatch):
    monitor = TelegramMonitor()
    replies = []

    async def fake_send(chat_id, text, reply_markup=None):
        replies.append(text)

    monkeypatch.setattr(monitor, "_send", fake_send)
    await monitor._handle_update({
        "message": {
            "chat": {"id": 1485409432, "type": "private"},
            "from": {"id": 1485409432},
            "text": long_post(),
            "message_id": 11,
        }
    })
    assert read_rows() == []
    await monitor._handle_update({
        "message": {
            "chat": {"id": 1485409432, "type": "private"},
            "from": {"id": 1485409432},
            "text": "/stop",
            "message_id": 12,
        }
    })
    assert read_rows() == []
