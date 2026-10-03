import os

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")

from delivery.telegram_transfer import format_transfer_ad
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
    assert "📌 Title: Small package from Tehran to Toronto" in text
    assert text.index("📌 Title:") < text.index("Origin:")
    assert "🇮🇷 Origin: Tehran" in text
    assert "🇨🇦 Destination: Toronto" in text


def test_missing_transfer_title_does_not_render_empty_title_line():
    record = _record(title=None)
    text = TelegramTransferPublisher.format_ad(record, record)

    assert "📌 Title:" not in text


def test_transfer_text_cleanup_preserves_ascii_t_characters():
    assert TelegramTransferPublisher._remove_emojis(
        "Small package from Tehran to Toronto"
    ) == "Small package from Tehran to Toronto"



def test_transfer_description_uses_exact_raw_user_text():
    raw = "#پذیرش_بار\nمبدا: چین ✈️\nتوضیحات :  پذیرش بار تا ۶۰ کیلو"
    record = _record(
        raw_text=raw,
        description="AI-generated rewritten description that must not be published",
        cargo_type="passenger cargo",
        weight=60,
        weight_unit="kilogram",
        departure_date="2026-10-10",
    )

    text = TelegramTransferPublisher.format_ad(record, record)

    assert f"📝 Description: {raw}" in text
    assert "AI-generated rewritten description" not in text
    assert "✈️" in text
    assert "📦 Cargo Type: passenger cargo" in text
    assert "⚖️ Weight: 60 kilogram" in text
    assert "📅 Date: 10/10/2026" in text
    assert "📝 توضیحات:" not in text


def test_legacy_transfer_formatter_uses_english_labels_and_raw_text():
    raw = "متن اصلی کاربر\nبدون بازنویسی ✅"
    text = format_transfer_ad({
        "origin_city": "China",
        "origin_country": "CN",
        "destination_city": "Iran",
        "destination_country": "IR",
        "cargo_type": "passenger cargo",
        "weight": 60,
        "weight_unit": "kilogram",
        "raw_text": raw,
        "description": "rewritten",
    })

    assert "📦 Cargo Type: passenger cargo" in text
    assert "⚖️ Weight: 60 kilogram" in text
    assert f"📝 Description:\n{raw}" in text
    assert "rewritten" not in text
    assert "نوع بار:" not in text
