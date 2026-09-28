"""Security regression tests for the monitoring bot's Telegram allowlist."""

import pytest

from monitoring.telegram_monitor import ADMIN_USER_IDS, TelegramMonitor


def update(user_id, *, chat_id=None, chat_type="private", command="/start"):
    return {
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
            "text": command,
        }
    }


@pytest.mark.asyncio
async def test_only_admin_ids_can_subscribe(monkeypatch):
    monitor = TelegramMonitor()
    subscriptions = []
    requests = []

    monkeypatch.setattr("monitoring.telegram_monitor.database.subscribe_monitor_chat",
                        lambda *args: subscriptions.append(args))

    async def fake_api(method, payload):
        requests.append((method, payload))

    monkeypatch.setattr(monitor, "_api", fake_api)

    assert ADMIN_USER_IDS == {1485409432, 266809220, 7469291969, 106056586}
    for user_id in ADMIN_USER_IDS:
        await monitor._handle_update(update(user_id))
    await monitor._handle_update(update(123456789))

    assert {item[0] for item in subscriptions} == ADMIN_USER_IDS
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_admin_commands_rejected_from_groups_and_other_users(monkeypatch):
    monitor = TelegramMonitor()
    subscriptions = []
    requests = []

    monkeypatch.setattr("monitoring.telegram_monitor.database.subscribe_monitor_chat",
                        lambda *args: subscriptions.append(args))

    async def fake_api(method, payload):
        requests.append((method, payload))

    monkeypatch.setattr(monitor, "_api", fake_api)
    await monitor._handle_update(update(1485409432, chat_id=-100123, chat_type="supergroup"))
    await monitor._handle_update(update(123456789, chat_id=1485409432))
    await monitor._handle_update(update(123456789, command="/database"))

    assert subscriptions == []
    assert requests == []


@pytest.mark.asyncio
async def test_old_unauthorized_subscribers_receive_no_reports(monkeypatch):
    monitor = TelegramMonitor()
    monitor.enabled = True
    requests = []

    monkeypatch.setattr("monitoring.telegram_monitor.database.get_monitor_subscribers",
                        lambda: [{"chat_id": 123456789}, {"chat_id": 266809220}])

    async def fake_api(method, payload):
        requests.append((method, payload))

    monkeypatch.setattr(monitor, "_api", fake_api)
    await monitor.broadcast("private report")
    await monitor._send(123456789, "private report")

    assert len(requests) == 1
    assert requests[0][1]["chat_id"] == 266809220


@pytest.mark.asyncio
async def test_callback_requires_admin_sender_in_private_chat(monkeypatch):
    monitor = TelegramMonitor()
    requested = []
    requests = []

    monkeypatch.setattr("monitoring.telegram_monitor.get_stage_control",
                        lambda: type("Control", (), {"request_skip": lambda _, stage: requested.append(stage)})())

    async def fake_api(method, payload):
        requests.append((method, payload))

    monkeypatch.setattr(monitor, "_api", fake_api)
    await monitor._handle_callback({
        "id": "bad", "from": {"id": 123456789},
        "message": {"chat": {"id": 1485409432, "type": "private"}},
        "data": "skip:ai",
    })
    await monitor._handle_callback({
        "id": "group", "from": {"id": 1485409432},
        "message": {"chat": {"id": -100123, "type": "supergroup"}},
        "data": "skip:ai",
    })

    assert requested == []
    assert requests == []
