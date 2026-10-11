import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from monitoring.transfer_live import _build_country_messages, _country_summary_rows


def test_country_summary_groups_active_rows_by_origin_and_destination():
    rows = [
        {"origin_country": "IR", "destination_country": "DE"},
        {"origin_country": "ir", "destination_country": "de"},
        {"origin_country": "TR", "destination_country": "DE"},
        {"origin_country": "IR", "destination_country": "CA"},
        {"origin_country": "", "destination_country": "CA"},
        {"origin_country": "IR", "destination_country": ""},
    ]

    assert _country_summary_rows(rows) == [
        (("IR", "CA"), 1),
        (("IR", "DE"), 2),
        (("TR", "DE"), 1),
    ]


def test_country_rich_message_contains_persian_heading_live_counts_and_embedded_buttons(monkeypatch):
    monkeypatch.setattr(
        "monitoring.transfer_live._fetch_active_country_summary",
        lambda: [(("IR", "DE"), 239), (("TR", "CA"), 1)],
    )
    monkeypatch.setattr(
        "monitoring.transfer_live._fetch_published_transfer_stats",
        lambda: {"latest_number": 9524, "last_7_days": 396, "last_30_days": 2167},
    )
    message = _build_country_messages()[0]["html"]

    assert "<h5>آگهی های فعال ارسال بار در ادورتیو</h5>" in message
    assert "<h1>" not in message
    assert "📊 آگهی های فعال: <b>240</b>" in message
    assert "📅 آگهی های ۷ روز گذشته: <b>396</b>" in message
    assert "📅 تعداد آگهی های ۳۰ روز گذشته: <b>2,167</b>" in message
    assert "📤 تعداد آگهی های ثبت شده: <b>9,524</b>" in message
    assert '<th align="right">مبدا</th>' in message
    assert '<th align="right">مقصد</th>' in message
    assert '<th align="center">آگهی</th>' in message
    assert "Open Requests" not in message
    assert "Iran 🇮🇷" in message
    assert "Germany 🇩🇪" in message
    assert "Turkey 🇹🇷" in message
    assert "Canada 🇨🇦" in message
    assert ">239<" in message
    assert ">1<" in message

    # Rich Message native buttons, not an external inline keyboard.
    assert message.count('<tg-button-row align="center">') == 1
    assert message.count('<tg-button type="url" style="primary"') == 2
    match = re.search(
        r'<tg-button type="url" style="primary" url="([^"]+)">📝 ثبت آگهی</tg-button>',
        message,
    )
    assert match is not None
    draft_url = unescape(match.group(1))
    parsed = urlsplit(draft_url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https", "t.me", "/advertio_support"
    )
    assert parse_qs(parsed.query) == {
        "text": ["سلام وقت بخیر درخواست ثبت آگهی ارسال بار/مسافر داشتم در کانال Advertio"]
    }
    assert " " not in draft_url
    assert "پشتیبان" not in message
    assert 'url="https://t.me/advertio_cargo">✈️ کانال ارسال بار هوایی</tg-button>' in message
    assert message.endswith("</tg-button-row>")


def test_country_rich_message_empty_state_keeps_heading_counters_and_buttons(monkeypatch):
    monkeypatch.setattr(
        "monitoring.transfer_live._fetch_active_country_summary", lambda: [],
    )
    monkeypatch.setattr(
        "monitoring.transfer_live._fetch_published_transfer_stats",
        lambda: {"latest_number": 9524, "last_7_days": 396, "last_30_days": 2167},
    )

    messages = _build_country_messages()
    assert len(messages) == 1
    message = messages[0]["html"]

    assert message.startswith("<h5>آگهی های فعال ارسال بار در ادورتیو</h5>")
    assert "📊 آگهی های فعال: <b>0</b>" in message
    assert "📅 آگهی های ۷ روز گذشته: <b>396</b>" in message
    assert "📅 تعداد آگهی های ۳۰ روز گذشته: <b>2,167</b>" in message
    assert "📤 تعداد آگهی های ثبت شده: <b>9,524</b>" in message
    assert "در حال حاضر آگهی فعال" in message
    assert "<table" not in message
    assert message.count('<tg-button type="url" style="primary"') == 2
    assert "📝 ثبت آگهی" in message
    assert "https://t.me/advertio_support?text=" in message
    assert "✈️ کانال ارسال بار هوایی" in message
    assert message.endswith("</tg-button-row>")


@pytest.mark.asyncio
async def test_country_command_sends_buttons_as_part_of_rich_message(monkeypatch):
    from monitoring.transfer_live import _send_transfer_country

    monkeypatch.setattr(
        "monitoring.transfer_live._fetch_active_country_summary", lambda: [],
    )
    monkeypatch.setattr(
        "monitoring.transfer_live._fetch_published_transfer_stats",
        lambda: {"latest_number": 0, "last_7_days": 0, "last_30_days": 0},
    )

    class Monitor:
        def __init__(self):
            self.calls = []

        async def _api(self, method, payload):
            self.calls.append((method, payload))

    monitor = Monitor()
    await _send_transfer_country(monitor, 123)
    assert len(monitor.calls) == 1
    method, payload = monitor.calls[0]
    assert method == "sendRichMessage"
    assert payload["chat_id"] == 123
    assert payload["rich_message"]["is_rtl"] is True
    assert "<tg-button-row" in payload["rich_message"]["html"]
    assert "📝 ثبت آگهی" in payload["rich_message"]["html"]
    assert "https://t.me/advertio_support?text=" in payload["rich_message"]["html"]
    assert "reply_markup" not in payload
