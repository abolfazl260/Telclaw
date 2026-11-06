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


def test_transfer_text_cleanup_preserves_normal_language_characters():
    value = "Small luggage written output فارسی 中文"
    assert TelegramTransferPublisher._remove_emojis(value) == value


def test_transfer_text_cleanup_removes_emojis_without_rewriting_words():
    assert TelegramTransferPublisher._clean_description(
        "#tag Small luggage ✈️ written output ✅"
    ) == "Small luggage written output"



def test_transfer_description_uses_cleaned_extracted_text_not_raw_source():
    raw = "#RAW_SHOULD_NOT_PUBLISH متن خام کاربر ✈️"
    extracted = "#پذیرش_بار مبدا: چین ✈️\nتوضیحات : پذیرش و ارسال بار مسافری تا ۶۰ کیلو"
    record = _record(
        raw_text=raw,
        description=extracted,
        cargo_type="passenger cargo",
        weight=60,
        weight_unit="kilogram",
        departure_date="2026-10-10",
    )

    text = TelegramTransferPublisher.format_ad(record, record)

    assert text.endswith("\n\nمبدا: چین\nتوضیحات : پذیرش و ارسال بار مسافری تا ۶۰ کیلو")
    assert "Description:" not in text
    assert "📝" not in text
    assert "توضیحات : پذیرش و ارسال بار مسافری تا ۶۰ کیلو" in text
    assert "#پذیرش_بار" not in text
    assert "#RAW_SHOULD_NOT_PUBLISH" not in text
    assert "✈️" not in text
    assert "📦 Cargo Type: passenger cargo" in text
    assert "⚖️ Weight: 60 kilogram" in text
    assert "📅 Date: 10/10/2026" in text
    assert "📝 توضیحات:" not in text


def test_realistic_transfer_description_is_the_final_plain_text_block():
    message = "پرواز از تهران به برلین ۱۳ نوامبر قبول مدارک و بار قانونی و قابل رویت تا ۱۵ کیلو"
    record = _record(
        ad_number=10397,
        title="Tehran to Berlin 13 November",
        origin_city="Tehran",
        origin_country="IR",
        destination_city="Berlin",
        destination_country="DE",
        cargo_type="legal cargo",
        weight=15,
        weight_unit="kg",
        departure_date="2026-11-13",
        description=message,
    )

    text = TelegramTransferPublisher.format_ad(record, record)

    assert text.startswith("TR-010397\n\n📌 Title: Tehran to Berlin 13 November")
    assert "📅 Date: 13/11/2026 | 22/08/1405" in text
    assert text.endswith("\n\n" + message)
    assert "Description:" not in text
    assert "📝" not in text


def test_legacy_transfer_formatter_uses_cleaned_extracted_description():
    text = format_transfer_ad({
        "origin_city": "China",
        "origin_country": "CN",
        "destination_city": "Iran",
        "destination_country": "IR",
        "cargo_type": "passenger cargo",
        "weight": 60,
        "weight_unit": "kilogram",
        "raw_text": "#raw متن خام ✅",
        "description": "#پذیرش_بار متن اصلاح شده کاربر ✅",
    })

    assert "🇨🇳 Origin: China" in text
    assert "🇮🇷 Destination: Iran" in text
    assert "📦 Cargo Type: passenger cargo" in text
    assert "⚖️ Weight: 60 kilogram" in text
    assert text.endswith("\n\nمتن اصلاح شده کاربر")
    assert "Description:" not in text
    assert "📝" not in text
    assert "#پذیرش_بار" not in text
    assert "#raw" not in text
    assert "✅" not in text
    assert "نوع بار:" not in text
