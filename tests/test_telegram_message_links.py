"""Source links for crawler records and Telegram admin validation warnings."""
import pytest

from monitoring.telegram_monitor import TelegramMonitor
from services.telegram_message_links import build_telegram_message_link


@pytest.mark.parametrize(("channel", "message_id", "channel_id", "stored", "expected"), [
    ("@torontosaleshome", 140715, None, None,
     "https://t.me/torontosaleshome/140715"),
    ("private-source", 140715, "-100987654321", None,
     "https://t.me/c/987654321/140715"),
    ("-100987654321", 140715, None, None,
     "https://t.me/c/987654321/140715"),
    ("", 140715, "987654321", None, "https://t.me/c/987654321/140715"),
    ("channel_one", 140715, None, "https://t.me/c/987654321/140715",
     "https://t.me/c/987654321/140715"),
    ("channel_one", 140715, None, "https://t.me/channel_two/140715",
     "https://t.me/channel_two/140715"),
    ("channel_one", 140715, None, "https://t.me/channel_two/999",
     "https://t.me/channel_one/140715"),
    ("channel_one", 140715, None, 'https://t.me/channel_two/140715" onclick="x',
     "https://t.me/channel_one/140715"),
    ("admin-forward:1485409432", 140715, 987654321, None, None),
    ("", 140715, None, None, None),
    ("channel_one", 0, None, None, None),
    ("channel_one", "abc", None, None, None),
    ("channel_one", True, None, None, None),
    ("bad-channel-name", 140715, None, None, None),
])
def test_message_links_are_validated_and_have_fallbacks(
    channel, message_id, channel_id, stored, expected
):
    assert build_telegram_message_link(
        channel, message_id, channel_id=channel_id, stored_link=stored
    ) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("context", "link"), [
    ({
        "original_channel_username": "torontosaleshome",
        "original_message_id": 140715,
    }, "https://t.me/torontosaleshome/140715"),
    ({
        "original_channel_username": "private-source",
        "original_channel_id": "-100987654321",
        "original_message_id": 140715,
    }, "https://t.me/c/987654321/140715"),
    ({
        "original_channel_username": "admin-forward:1234",
        "original_message_id": 140715,
    }, None),
])
async def test_admin_original_message_contains_safe_link_after_id(context, link):
    monitor = TelegramMonitor()
    sent = []

    async def capture(text):
        sent.append(text)

    monitor.broadcast_admin_errors = capture
    await monitor._broadcast_original_message({
        "original_message_text": "🏠 Toronto 2-bedroom <available> & affordable",
        **context,
    })
    assert len(sent) == 1
    body = sent[0]
    assert "Original Telegram Message" in body
    assert "&lt;available&gt; &amp; affordable" in body
    assert "<b>Message ID:</b> 140715" in body
    if link is None:
        assert "Open original message" not in body
    else:
        assert f'href="{link}"' in body
        assert body.index("Message ID:") < body.index("Open original message")
        assert body.index("Open original message") < body.index("<pre>")
