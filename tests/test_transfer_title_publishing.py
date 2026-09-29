import os

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")

from delivery.telegram_transfer_publisher import TelegramTransferPublisher


def _record(**overrides):
    data = {
        "ad_number": 8601,
        "title": "Small package from Tehran to Toronto",
        "origin_city": "Tehran",
        "origin_country": "IR",
        "destination_city": "Toronto",
        "destination_country": "CA",
        "description": "Small package available.",
    }
    data.update(overrides)
    return data


def test_transfer_title_is_published_after_ad_number():
    record = _record()
    text = TelegramTransferPublisher.format_ad(record, record)
    lines = text.splitlines()

    assert lines[0] == "TR-008601"
    assert "📌 عنوان: Small package from Tehran to Toronto" in text
    assert text.index("📌 عنوان:") < text.index("مبدا:")


def test_missing_transfer_title_does_not_render_empty_title_line():
    record = _record(title=None)
    text = TelegramTransferPublisher.format_ad(record, record)

    assert "📌 عنوان:" not in text


def test_transfer_text_cleanup_preserves_ascii_t_characters():
    assert TelegramTransferPublisher._remove_emojis(
        "Small package from Tehran to Toronto"
    ) == "Small package from Tehran to Toronto"
