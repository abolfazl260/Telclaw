"""Integration checks for the monitoring bot's topic-aware send paths."""
import pytest

from monitoring.telegram_monitor import TelegramMonitor, TelegramAPIError
from monitoring.private_topics import PrivateTopics
from monitoring import transfer_live


@pytest.mark.asyncio
async def test_monitor_routes_send_methods_and_preserves_incoming_thread(monkeypatch):
    monitor = TelegramMonitor()
    monitor._topics.ready = True
    monkeypatch.setattr(
        monitor._topics, "_lookup",
        lambda chat_id, key: {"dashboard": 101, "warning": 102, "error": 103}[key],
    )
    calls = []

    async def fake_raw(method, payload=None):
        calls.append((method, dict(payload or {})))
        return {"ok": True, "result": {"message_id": len(calls)}}

    monkeypatch.setattr(monitor, "_api_raw", fake_raw)
    admin_id = next(iter(sorted(monitor.ADMIN_USER_IDS))) if hasattr(monitor, "ADMIN_USER_IDS") else 1485409432

    await monitor._api("sendMessage", {"chat_id": admin_id, "text": "report"})
    assert calls[-1][1]["message_thread_id"] == 101

    with monitor._topics.reply_in_thread(444):
        await monitor._api("sendRichMessage", {"chat_id": admin_id, "rich_message": {"html": "<p>x</p>"}})
        assert calls[-1][1]["message_thread_id"] == 444
        with monitor._topics.route("warning"):
            await monitor._api("sendWidget", {"chat_id": admin_id, "html": "<p>x</p>"})
            assert calls[-1][1]["message_thread_id"] == 102

    await monitor._api("sendMessage", {"chat_id": 999999, "text": "public"})
    assert "message_thread_id" not in calls[-1][1]


@pytest.mark.asyncio
async def test_monitor_invalid_topic_falls_back_once_without_resending_timeouts(monkeypatch):
    monitor = TelegramMonitor()
    monitor._topics.ready = True
    monkeypatch.setattr(monitor._topics, "_lookup", lambda chat_id, key: 101)
    invalidated = []
    monkeypatch.setattr(
        monitor._topics, "invalidate_thread",
        lambda chat_id, thread_id: invalidated.append((chat_id, thread_id)),
    )
    calls = []

    async def fake_raw(method, payload=None):
        calls.append(dict(payload))
        if payload.get("message_thread_id"):
            raise TelegramAPIError(
                method, 400, {"error_code": 400,
                              "description": "Bad Request: message thread not found"}
            )
        return {"ok": True, "result": {"message_id": 123}}

    monkeypatch.setattr(monitor, "_api_raw", fake_raw)
    await monitor._api("sendMessage", {"chat_id": 1485409432, "text": "alert"})
    assert len(calls) == 2
    assert calls[0]["message_thread_id"] == 101
    assert "message_thread_id" not in calls[1]
    assert invalidated == [(1485409432, 101)]

    async def timeout_raw(method, payload=None):
        raise TimeoutError("ambiguous delivery")

    monkeypatch.setattr(monitor, "_api_raw", timeout_raw)
    with pytest.raises(TimeoutError):
        await monitor._api("sendMessage", {"chat_id": 1485409432, "text": "alert"})


@pytest.mark.asyncio
async def test_transfer_live_extension_preserves_command_topic(monkeypatch):
    class LocalMonitor:
        def __init__(self):
            self._topics = PrivateTopics(self)
            self._topics.ready = True
            self.delivered_threads = []

        @staticmethod
        def _is_admin_chat(chat_id):
            return chat_id == 1485409432

        @staticmethod
        def _is_admin_private_chat(chat, user):
            return chat.get("type") == "private" and user.get("id") == chat.get("id")

        @staticmethod
        def _is_subscribed(chat_id):
            return True

        async def _handle_update(self, update):
            pass

    async def fake_transfer_report(monitor, chat_id):
        monitor.delivered_threads.append(monitor._topics.target_thread(chat_id))

    monitor = LocalMonitor()
    monkeypatch.setattr(monitor._topics, "_lookup", lambda chat_id, key: 101)
    monkeypatch.setattr(transfer_live, "_send_transfer_live", fake_transfer_report)
    transfer_live.install_transfer_live_command(monitor)

    await monitor._handle_update({"message": {
        "chat": {"id": 1485409432, "type": "private"},
        "from": {"id": 1485409432},
        "text": "/transferlive",
        "message_thread_id": 777,
    }})
    assert monitor.delivered_threads == [777]
