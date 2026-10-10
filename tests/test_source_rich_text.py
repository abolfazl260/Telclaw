"""Regression tests for the rich-text Telegram /source directory."""
import json
from html.parser import HTMLParser

import pytest

import config
from monitoring.source_formatter import format_source_messages
from monitoring.telegram_monitor import ADMIN_USER_IDS, TelegramMonitor


class _TelegramHtmlValidator(HTMLParser):
    def __init__(self):
        super().__init__()
        self.open_tags = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "a", "code"}
        self.open_tags.append(tag)
        if tag == "a":
            assert dict(attrs).get("href", "").startswith("https://t.me/")

    def handle_endtag(self, tag):
        assert self.open_tags and self.open_tags.pop() == tag


def _assert_valid_pages(pages):
    for page in pages:
        assert len(page) < 3900
        parser = _TelegramHtmlValidator()
        parser.feed(page)
        parser.close()
        assert parser.open_tags == []


def test_rich_source_output_has_group_counts_clickable_names_and_escaped_html():
    data = {
        "Canada_Job": [
            {
                "name": "Jobs & <Hiring>",
                "username": "@Toronto_Jobs",
                "description": "Work <now> & earn a living",
            },
            {"name": "Second jobs", "username": "TorontoHiring", "description": ""},
        ],
        "Canada_Rent": [
            {"name": "Homes", "username": "RentToronto", "description": "Apartments"},
        ],
    }
    pages = format_source_messages(data)
    assert len(pages) == 1
    html = pages[0]
    assert "<b>Categories:</b> 2" in html
    assert "<b>Sources:</b> 3" in html
    assert "<b>Canada_Job</b>" in html
    assert "<b>Canada_Rent</b>" in html
    assert "<i>· 2 sources</i>" in html
    assert '<a href="https://t.me/Toronto_Jobs">Jobs &amp; &lt;Hiring&gt;</a>' in html
    assert "<code>@Toronto_Jobs</code>" in html
    assert "<i>Work &lt;now&gt; &amp; earn a living</i>" in html
    assert "Page 1/1" in html
    _assert_valid_pages(pages)


def test_large_source_directory_splits_between_complete_entries():
    data = {
        "Canada_Flight": [
            {
                "name": f"Channel #{i}",
                "username": f"flights_{i:04d}",
                "description": "Flight and passenger cargo messages with some details",
            }
            for i in range(1, 180)
        ],
        "Canada_Rent": [
            {"name": "Some Homes", "username": "HomesCanada"}
        ],
    }
    pages = format_source_messages(data)
    assert len(pages) > 2
    assert "<b>Sources:</b> 180" in pages[0]
    _assert_valid_pages(pages)
    for i, page in enumerate(pages, 1):
        assert f"Page {i}/{len(pages)}" in page
    all_html = "\n".join(pages)
    for i in range(1, 180):
        assert all_html.count(f'<code>@flights_{i:04d}</code>') == 1
    assert '<a href="https://t.me/HomesCanada">' in all_html
    assert "continued" in all_html


def test_untrusted_usernames_never_generate_untrusted_links():
    data = {
        "<bad> & category": [
            {"name": "<script>alert(1)</script>", "username": 'bad" onclick="oops',
             "description": "<b>not a tag</b>"},
            {"name": "", "username": None},
        ],
        "Empty": [],
    }
    pages = format_source_messages(data)
    all_html = "\n".join(pages)
    assert "<script>" not in all_html
    assert 'onclick="' not in all_html
    assert "&lt;script&gt;" in all_html
    assert "&lt;bad&gt; &amp; category" in all_html
    assert "No channels configured" in all_html
    assert "<b>Sources:</b> 2" in all_html
    _assert_valid_pages(pages)


@pytest.mark.asyncio
async def test_source_command_uses_telegram_html_and_only_admin_private_chat(tmp_path, monkeypatch):
    channels_path = tmp_path / "sources.json"
    channels_path.write_text(
        json.dumps({
            "Canada_Job": [
                {"name": f"Job {i}", "username": f"jobs_{i:04d}"}
                for i in range(100)
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CHANNELS_JSON", str(channels_path))
    monitor = TelegramMonitor()
    calls = []

    async def fake_api(method, payload):
        calls.append((method, payload))
        return {"ok": True}

    monkeypatch.setattr(monitor, "_api", fake_api)

    def command(uid, chat_type="private"):
        return {
            "message": {
                "from": {"id": uid},
                "chat": {"id": uid, "type": chat_type},
                "text": "/source",
            }
        }

    await monitor._handle_update(command(123456789))
    await monitor._handle_update(command(next(iter(ADMIN_USER_IDS)), "supergroup"))
    assert calls == []
    admin = sorted(ADMIN_USER_IDS)[0]
    await monitor._handle_update(command(admin))
    messages = [payload for method, payload in calls if method == "sendMessage"]
    assert len(messages) >= 2
    assert all(msg["chat_id"] == admin and msg["parse_mode"] == "HTML"
               for msg in messages)
    _assert_valid_pages([msg["text"] for msg in messages])


@pytest.mark.asyncio
async def test_source_command_reports_bad_config_without_sending_partial_pages(tmp_path, monkeypatch):
    config_path = tmp_path / "sources.json"
    config_path.write_text('{"Category": false}', encoding="utf-8")
    monkeypatch.setattr(config, "CHANNELS_JSON", str(config_path))
    monitor = TelegramMonitor()
    sent = []

    async def fake_send(chat_id, text):
        sent.append(text)

    monkeypatch.setattr(monitor, "_send", fake_send)
    await monitor._send_source_chunks(sorted(ADMIN_USER_IDS)[0])
    assert len(sent) == 1
    assert "Source file error" in sent[0]
