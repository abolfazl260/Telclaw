"""Admin error notifications: worker-thread forwarding, security and flood control."""
import asyncio
import logging
import threading
import time

import pytest

import config
from monitoring.error_alerts import ErrorAlertDispatcher, redact_secrets
from monitoring.telegram_monitor import TelegramMonitor
from storage import database


@pytest.fixture
def monitor_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "notifications.db"))
    monkeypatch.setattr(config, "TELEGRAM_MONITOR_ENABLED", True)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "12345:notification-secret-token")
    database.initialize_db()
    return None


@pytest.mark.asyncio
async def test_background_worker_error_reaches_only_subscribed_admin(monitor_db, monkeypatch):
    monitor = TelegramMonitor()
    calls = []
    async def fake_api(method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": []}

    async def idle_poll():
        await asyncio.Event().wait()

    monkeypatch.setattr(monitor, "_api", fake_api)
    monkeypatch.setattr(monitor, "_poll_updates", idle_poll)
    monkeypatch.setattr(
        "monitoring.telegram_monitor.database.get_monitor_subscribers",
        lambda: [{"chat_id": 266809220}, {"chat_id": 123456789}],
    )
    before = set(logging.getLogger().handlers)
    await monitor.start()
    try:
        def emit_from_worker():
            logging.getLogger("telclaw.test.worker").error(
                "Worker crashed while parsing message_id=%s", 98311
            )
        thread = threading.Thread(target=emit_from_worker)
        thread.start()
        thread.join()
        await asyncio.sleep(0.03)
        await asyncio.wait_for(monitor._error_dispatcher.queue.join(), timeout=2)

        notifications = [
            payload for method, payload in calls if method == "sendMessage"
        ]
        assert len(notifications) == 1
        assert notifications[0]["chat_id"] == 266809220
        text = notifications[0]["text"]
        assert "telclaw.test.worker" in text
        assert "Worker crashed" in text
        assert "98311" in text

        with database.get_connection() as conn:
            activity = conn.execute(
                "SELECT source,message FROM system_activity WHERE kind='error'"
            ).fetchall()
        assert len(activity) == 1
        assert activity[0]["source"] == "telclaw.test.worker"
    finally:
        await monitor.stop()
    assert set(logging.getLogger().handlers) == before


@pytest.mark.asyncio
async def test_duplicate_errors_are_throttled_then_report_suppressed_count():
    reports = []
    class FakeMonitor:
        async def error(self, level, source, message):
            reports.append((level, source, message))
    dispatcher = ErrorAlertDispatcher(FakeMonitor(), repeat_seconds=300)
    dispatcher.start()
    try:
        dispatcher.submit("ERROR", "telclaw.ai", "Identical provider failure")
        dispatcher.submit("ERROR", "telclaw.ai", "Identical provider failure")
        dispatcher.submit("ERROR", "telclaw.ai", "Identical provider failure")
        await asyncio.sleep(0.03)
        await asyncio.wait_for(dispatcher.queue.join(), timeout=2)
        assert len(reports) == 1

        key = ("ERROR", "telclaw.ai", "Identical provider failure")
        assert dispatcher.suppressed[key] == 2
        dispatcher.last_sent[key] = time.monotonic() - 301
        dispatcher.submit("ERROR", "telclaw.ai", "Identical provider failure")
        await asyncio.sleep(0.03)
        await asyncio.wait_for(dispatcher.queue.join(), timeout=2)
        assert len(reports) == 2
        assert "Similar errors suppressed: 2" in reports[1][2]
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_errors_from_different_message_ids_are_grouped():
    reports = []
    class FakeMonitor:
        async def error(self, level, source, message):
            reports.append(message)
    dispatcher = ErrorAlertDispatcher(FakeMonitor())
    dispatcher.start()
    try:
        dispatcher.submit("ERROR", "telclaw.processing",
                          "Failed message_id=100 channel=abc reason=timeout")
        dispatcher.submit("ERROR", "telclaw.processing",
                          "Failed message_id=101 channel=abc reason=timeout")
        await asyncio.sleep(0.03)
        await asyncio.wait_for(dispatcher.queue.join(), timeout=2)
        assert len(reports) == 1
        assert "message_id=100" in reports[0]
    finally:
        await dispatcher.stop()


def test_configured_credentials_are_removed_from_error_alert(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:VerySensitiveToken")
    monkeypatch.setattr(config, "GROQ_PROVIDERS", [
        {"api_key": "super-groq-secret-key", "model": "sample"}
    ])
    monkeypatch.setattr(config, "CLOUDFLARE_PROVIDERS", [
        {"api_token": "super-cloudflare-secret-key", "account_id": "sample"}
    ])
    text = redact_secrets(
        "Request to 123:VerySensitiveToken failed with "
        "super-groq-secret-key, super-cloudflare-secret-key"
    )
    assert "VerySensitiveToken" not in text
    assert "super-groq-secret-key" not in text
    assert "super-cloudflare-secret-key" not in text
    assert text.count("[REDACTED]") == 3


@pytest.mark.asyncio
async def test_queue_overflow_is_bounded_and_shutdown_is_safe():
    class FakeMonitor:
        async def error(self, level, source, message):
            return None
    dispatcher = ErrorAlertDispatcher(FakeMonitor(), max_pending=1)
    dispatcher._enqueue("ERROR", "test", "first")
    dispatcher._enqueue("ERROR", "test", "second")
    assert dispatcher.queue.qsize() == 1
    assert dispatcher.dropped == 1
    await dispatcher.stop()
    assert dispatcher.stopped


@pytest.mark.asyncio
async def test_monitor_disabled_installs_no_error_handler(monkeypatch):
    monitor = TelegramMonitor()
    monitor.enabled = False
    root = logging.getLogger()
    before = list(root.handlers)
    await monitor.start()
    assert list(root.handlers) == before
    assert monitor._error_dispatcher is None
