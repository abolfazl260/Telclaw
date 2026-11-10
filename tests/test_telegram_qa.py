"""Public /qa native rich-accordion contract and access-control tests."""

from html.parser import HTMLParser

import pytest

from monitoring.qa import FAQ_SECTIONS, build_qa_rich_message
from monitoring.telegram_monitor import ADMIN_USER_IDS, TelegramMonitor


class QAHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.details = 0
        self.summaries = 0
        self.urls = []

    def handle_starttag(self, tag, attrs):
        assert tag in {
            "h1", "h2", "p", "details", "summary", "a", "b", "i",
            "ul", "li", "tg-button-row", "tg-button",
        }
        attrs = dict(attrs)
        if tag == "details":
            assert "open" not in attrs  # all FAQs start collapsed
            self.details += 1
        if tag == "summary":
            assert self.stack and self.stack[-1] == "details"
            self.summaries += 1
        if tag == "a":
            assert attrs["href"].startswith("https://t.me/")
            self.urls.append(attrs["href"])
        if tag == "tg-button":
            assert attrs["type"] == "url"
            self.urls.append(attrs["url"])
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag


def _update(user_id, command="/qa", *, chat_id=None, chat_type="private", forwarded=False):
    msg = {
        "from": {"id": user_id},
        "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
        "text": command,
    }
    if forwarded:
        msg["forward_origin"] = {"type": "user", "sender_user": {"id": 98}}
    return {"message": msg}


def test_qa_has_all_questions_in_three_sections_and_native_details_blocks():
    message = build_qa_rich_message()
    assert set(message) == {"html", "is_rtl", "skip_entity_detection"}
    assert message["is_rtl"] is True
    assert message["skip_entity_detection"] is True
    assert len(FAQ_SECTIONS) == 3
    assert [len(questions) for _, questions in FAQ_SECTIONS] == [14, 8, 1]
    assert sum(len(questions) for _, questions in FAQ_SECTIONS) == 23
    content = message["html"]
    assert len(content) < 32768

    parser = QAHtmlParser()
    parser.feed(content)
    parser.close()
    assert not parser.stack
    assert parser.details == parser.summaries == 23
    assert len(parser.urls) >= 5

    for section, questions in FAQ_SECTIONS:
        assert section in content
        for question, _ in questions:
            assert question in content
    assert "هر هفته سه‌شنبه" in content
    assert "وکیوم ضدآب" in content
    assert "به معنی تضمین امنیت معامله نیست" in content
    assert "https://t.me/advertio_support" in parser.urls
    assert "https://t.me/advertio_bot" in parser.urls


@pytest.mark.asyncio
async def test_qa_public_in_own_private_chat_without_subscribing(monkeypatch):
    monitor = TelegramMonitor()
    calls = []

    async def api(method, payload):
        calls.append((method, payload))
        return {"ok": True}

    monkeypatch.setattr(monitor, "_api", api)
    public_id = 123456789
    assert public_id not in ADMIN_USER_IDS

    await monitor._handle_update(_update(public_id))
    await monitor._handle_update(_update(min(ADMIN_USER_IDS), "/qa@TelclawBot"))
    assert len(calls) == 2
    for method, payload in calls:
        assert method == "sendRichMessage"
        assert payload["rich_message"] == build_qa_rich_message()
        assert set(payload) == {"chat_id", "rich_message"}
    assert calls[0][1]["chat_id"] == public_id


@pytest.mark.asyncio
async def test_qa_blocked_in_groups_spoofed_chats_and_forwards(monkeypatch):
    monitor = TelegramMonitor()
    calls = []

    async def api(method, payload):
        calls.append((method, payload))

    monkeypatch.setattr(monitor, "_api", api)
    await monitor._handle_update(_update(123456789, chat_type="group"))
    await monitor._handle_update(_update(123456789, chat_id=-10025, chat_type="supergroup"))
    await monitor._handle_update(_update(123456789, chat_id=266809220))
    await monitor._handle_update(_update(123456789, forwarded=True))
    assert calls == []


@pytest.mark.asyncio
async def test_qa_menu_registered_for_default_and_private_scope(monkeypatch):
    monitor = TelegramMonitor()
    calls = []

    async def api(method, payload):
        calls.append((method, payload))

    monkeypatch.setattr(monitor, "_api", api)
    await monitor._register_commands()
    assert [payload["scope"]["type"] for _, payload in calls] == [
        "default", "all_private_chats"
    ]
    for method, payload in calls:
        assert method == "setMyCommands"
        commands = [row["command"] for row in payload["commands"]]
        assert commands.count("qa") == 1
        assert {"aboutus", "status", "transferlive", "database"} <= set(commands)
