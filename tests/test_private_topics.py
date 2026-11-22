"""Regression tests for the optional three-topic private Telegram sidebar."""
import asyncio
import sqlite3

import pytest

from monitoring.private_topics import PrivateTopics, TOPICS
from storage import database


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "topics.db"
    monkeypatch.setattr(database, "get_connection", lambda: sqlite3.connect(path))
    return path


class FakeMonitor:
    def __init__(self):
        self.calls = []
        self.topic_number = 100

    @staticmethod
    def _is_admin_chat(chat_id):
        return int(chat_id) in {11, 22}

    async def _api(self, method, payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"result": {"has_topics_enabled": False, "has_topic_sidebar": False,
                               "allows_users_to_create_topics": True}}
        if method == "createForumTopic":
            self.topic_number += 1
            return {"result": {"message_thread_id": self.topic_number}}
        return {"ok": True, "result": True}


@pytest.mark.asyncio
async def test_provisions_three_topics_once_per_admin(db_path):
    monitor = FakeMonitor()
    topics = PrivateTopics(monitor, enabled=True)
    await topics.configure()
    await topics.ensure_for_admin(11)
    await topics.ensure_for_admin(11)
    await topics.ensure_for_admin(22)
    assert len([c for c in monitor.calls if c[0] == "createForumTopic"]) == 6
    assert [c[1]["name"] for c in monitor.calls if c[0] == "createForumTopic"][:3] == [name for _, name in TOPICS]
    assert len([c for c in monitor.calls if c[0] == "setMySettings"]) == 1
    assert topics.target_thread(11) == 101
    assert topics.target_thread(22) == 104


@pytest.mark.asyncio
async def test_provisioning_persists_across_restart(db_path):
    first = FakeMonitor()
    topics = PrivateTopics(first, enabled=True)
    await topics.configure()
    await topics.ensure_for_admin(11)
    second = FakeMonitor()
    reloaded = PrivateTopics(second, enabled=True)
    await reloaded.configure()
    await reloaded.ensure_for_admin(11)
    assert not any(method == "createForumTopic" for method, _ in second.calls)
    assert reloaded.target_thread(11) == 101


@pytest.mark.asyncio
async def test_concurrent_provisioning_creates_each_topic_once(db_path):
    monitor = FakeMonitor()
    topics = PrivateTopics(monitor, enabled=True)
    await topics.configure()
    await asyncio.gather(topics.ensure_for_admin(11), topics.ensure_for_admin(11))
    assert len([method for method, _ in monitor.calls if method == "createForumTopic"]) == 3


@pytest.mark.asyncio
async def test_route_priority_and_restore(db_path):
    monitor = FakeMonitor()
    topics = PrivateTopics(monitor, enabled=True)
    await topics.configure()
    await topics.ensure_for_admin(11)
    with topics.reply_in_thread(999):
        assert topics.target_thread(11) == 999
        with topics.route("warning"):
            assert topics.target_thread(11) == 102
        with topics.route("error"):
            assert topics.target_thread(11) == 103
        assert topics.target_thread(11) == 999
    assert topics.target_thread(11) == 101
    assert topics.target_thread(33) is None


@pytest.mark.asyncio
async def test_invalidated_topic_is_recreated_only_on_next_start(db_path):
    monitor = FakeMonitor()
    topics = PrivateTopics(monitor, enabled=True)
    await topics.configure()
    await topics.ensure_for_admin(11)
    topics.invalidate_thread(11, 102)
    with topics.route("warning"):
        assert topics.target_thread(11) is None
    await topics.ensure_for_admin(11)
    assert topics.target_thread(11) == 101
    with topics.route("warning"):
        assert topics.target_thread(11) == 104
    assert len([c for c in monitor.calls if c[0] == "createForumTopic"]) == 4


def test_fallback_only_after_known_invalid_topic_error(db_path):
    class Error(Exception):
        def __init__(self, status, detail):
            super().__init__(detail)
            self.status = status

    assert PrivateTopics.is_invalid_thread(Error(400, "Bad Request: message thread not found"))
    assert not PrivateTopics.is_invalid_thread(Error(429, "Too Many Requests"))
    assert not PrivateTopics.is_invalid_thread(Error(500, "message thread not found"))
    assert not PrivateTopics.is_invalid_thread(TimeoutError())


@pytest.mark.asyncio
async def test_disabled_does_not_change_bot_or_provision(db_path):
    monitor = FakeMonitor()
    topics = PrivateTopics(monitor, enabled=False)
    await topics.configure()
    await topics.ensure_for_admin(11)
    assert topics.target_thread(11) is None
    assert monitor.calls == []


@pytest.mark.asyncio
async def test_existing_telegram_settings_are_preserved(db_path):
    class AlreadyEnabled(FakeMonitor):
        async def _api(self, method, payload):
            if method == "getMe":
                return {"result": {"has_topics_enabled": True, "has_topic_sidebar": True,
                                   "allows_users_to_create_topics": False}}
            return await super()._api(method, payload)

    monitor = AlreadyEnabled()
    topics = PrivateTopics(monitor, enabled=True)
    await topics.configure()
    assert topics.ready
    assert not any(method == "setMySettings" for method, _ in monitor.calls)
