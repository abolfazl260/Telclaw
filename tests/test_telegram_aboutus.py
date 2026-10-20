"""Public Advertio /aboutus directory and admin-boundary regression tests."""

from html.parser import HTMLParser

import pytest

from monitoring.aboutus import (
    BOTS,
    CHANNELS,
    GROUPS,
    SUPPORT,
    aboutus_keyboard,
    build_aboutus_message,
)
from monitoring.telegram_monitor import ADMIN_USER_IDS, TelegramMonitor


class TelegramHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.open_tags = []
        self.urls = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "a"}
        self.open_tags.append(tag)
        if tag == "a":
            attrs = dict(attrs)
            assert set(attrs) == {"href"}
            assert attrs["href"].startswith("https://t.me/")
            self.urls.append(attrs["href"])

    def handle_endtag(self, tag):
        assert self.open_tags and self.open_tags.pop() == tag


def _command(user_id, command="/aboutus", chat_id=None, chat_type="private"):
    return {
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id if chat_id is None else chat_id,
                     "type": chat_type},
            "text": command,
        }
    }


def test_aboutus_html_has_all_registry_assets_and_fits_telegram_message():
    text = build_aboutus_message()
    assert len(text) < 4096
    assert "Advertio | ادورتیو" in text
    assert "<b>" in text and "<i>" in text
    parser = TelegramHTMLParser()
    parser.feed(text)
    parser.close()
    assert parser.open_tags == []
    usernames = [username for _, username in (*CHANNELS, *GROUPS, *BOTS, SUPPORT)]
    assert len(CHANNELS) == 7
    assert len(GROUPS) == 5
    assert len(BOTS) == 2
    assert len(set(usernames)) == 15
    assert set(parser.urls) == {f"https://t.me/{username}" for username in usernames}
    assert "https://t.me/AdvertioBot" not in parser.urls


def test_aboutus_member_counts_and_each_link_is_on_its_own_line():
    text = build_aboutus_message()
    assert "مسکن | حدود 5K عضو" in text
    assert "ارسال بار | حدود 9K عضو" in text
    assert "۳ لینک" not in text and "۵ گروه" not in text
    assert "<b>پرواز و ویزا</b>" in text
    for _, username in (*CHANNELS, *GROUPS, *BOTS):
        assert sum(f'https://t.me/{username}' in line for line in text.splitlines()) == 1
    assert all(
        line.startswith("• <a href=")
        for line in text.splitlines()
        if '<a href="https://t.me/' in line and "پشتیبانی" not in line
    )


def test_aboutus_button_labels_and_priorities():
    rows = aboutus_keyboard()["inline_keyboard"]
    assert [item["url"] for item in rows[0]] == [
        "https://t.me/canadahouse24", "https://t.me/advertio_cargo"
    ]
    assert [item["url"] for item in rows[1]] == [
        "https://t.me/Flightiranbot", "https://t.me/advertio_bot"
    ]
    assert [item["url"] for item in rows[2]] == [
        "https://t.me/Ontario_Job", "https://t.me/advertio_shop"
    ]
    assert rows[1][1]["text"] == "🤖 ثبت آگهی"
    assert all("ثبت و مشاهده آگهی" not in item["text"] for row in rows for item in row)


def test_aboutus_inline_keyboard_only_has_valid_direct_links():
    rows = aboutus_keyboard()["inline_keyboard"]
    assert len(rows) == 4
    assert all(len(row) == 2 for row in rows)
    urls = [item["url"] for row in rows for item in row]
    assert len(urls) == len(set(urls))
    assert all(set(item) == {"text", "url"} for row in rows for item in row)
    text_urls = {
        f"https://t.me/{username}"
        for _, username in (*CHANNELS, *GROUPS, *BOTS, SUPPORT)
    }
    assert set(urls) <= text_urls


@pytest.mark.asyncio
async def test_aboutus_command_is_accessible_to_public_private_chat(monkeypatch):
    monitor = TelegramMonitor()
    requests = []

    async def fake_api(method, payload):
        requests.append((method, payload))
        return {"ok": True}

    monkeypatch.setattr(monitor, "_api", fake_api)
    public_id = 123456789
    assert public_id not in ADMIN_USER_IDS

    await monitor._handle_update(_command(public_id))
    await monitor._handle_update(_command(min(ADMIN_USER_IDS), "/aboutus@ExampleBot"))

    assert len(requests) == 2
    for method, payload in requests:
        assert method == "sendMessage"
        assert payload["parse_mode"] == "HTML"
        assert payload["disable_web_page_preview"] is True
        assert payload["text"] == build_aboutus_message()
        assert payload["reply_markup"] == aboutus_keyboard()
    assert requests[0][1]["chat_id"] == public_id


@pytest.mark.asyncio
async def test_aboutus_is_not_posted_to_groups_or_spoofed_private_chats(monkeypatch):
    monitor = TelegramMonitor()
    requests = []

    async def fake_api(method, payload):
        requests.append((method, payload))

    monkeypatch.setattr(monitor, "_api", fake_api)
    await monitor._handle_update(_command(123456789, chat_id=-100123, chat_type="supergroup"))
    await monitor._handle_update(_command(123456789, chat_id=266809220))
    await monitor._handle_update(_command(123456789, chat_type="group"))
    assert requests == []


@pytest.mark.asyncio
async def test_public_aboutus_does_not_open_admin_commands(monkeypatch):
    monitor = TelegramMonitor()
    requests = []

    async def fake_api(method, payload):
        requests.append((method, payload))

    monkeypatch.setattr(monitor, "_api", fake_api)
    for cmd in ("/status", "/database", "/health", "/start", "/backoffice", "/source", "/down_errors"):
        await monitor._handle_update(_command(123456789, cmd))
    assert requests == []


@pytest.mark.asyncio
async def test_aboutus_command_is_registered(monkeypatch):
    monitor = TelegramMonitor()
    registered = []

    async def fake_api(method, payload):
        registered.append((method, payload))

    monkeypatch.setattr(monitor, "_api", fake_api)
    await monitor._register_commands()

    assert len(registered) == 1
    method, payload = registered[0]
    assert method == "setMyCommands"
    commands = [item["command"] for item in payload["commands"]]
    assert commands.count("aboutus") == 1
    assert "database" in commands and "status" in commands
