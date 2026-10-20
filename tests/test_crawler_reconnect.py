"""Reconnect regression tests for Telethon collection without network access."""

import asyncio
from types import SimpleNamespace

import pytest

from collection import crawler


@pytest.fixture
def backoff_delays(monkeypatch):
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(crawler.asyncio, "sleep", fake_sleep)
    return delays


@pytest.mark.asyncio
async def test_connected_client_does_not_reconnect(backoff_delays):
    class Client:
        def __init__(self):
            self.connect_calls = 0

        def is_connected(self):
            return True

        async def connect(self):
            self.connect_calls += 1

        async def get_input_entity(self, channel):
            return channel

        async def iter_messages(self, _entity):
            yield SimpleNamespace(id=2)
            yield SimpleNamespace(id=1)

    client = Client()
    assert await crawler._get_input_entity_with_reconnect(client, "source") == "source"
    messages = [m.id async for m in crawler._iter_messages_with_reconnect(client, "source", "source")]

    assert messages == [2, 1]
    assert client.connect_calls == 0
    assert backoff_delays == []


@pytest.mark.asyncio
async def test_disconnected_client_reconnects_before_channel_lookup(backoff_delays):
    class Client:
        connected = False
        connect_calls = 0

        def is_connected(self):
            return self.connected

        async def connect(self):
            self.connect_calls += 1
            self.connected = True

        async def get_input_entity(self, channel):
            assert self.connected
            return channel

    client = Client()
    assert await crawler._get_input_entity_with_reconnect(client, "source") == "source"
    assert client.connect_calls == 1
    assert backoff_delays == [1]


@pytest.mark.asyncio
async def test_mid_stream_disconnect_resumes_after_last_yielded_id(backoff_delays):
    class Client:
        def __init__(self):
            self.connected = True
            self.disconnect_once = True
            self.connect_calls = 0
            self.offsets = []

        def is_connected(self):
            return self.connected

        async def connect(self):
            self.connect_calls += 1
            self.connected = True

        async def iter_messages(self, _entity, *, offset_id=None):
            self.offsets.append(offset_id)
            for message_id in (303, 302, 301):
                if offset_id is not None and message_id >= offset_id:
                    continue
                if message_id == 302 and self.disconnect_once:
                    self.disconnect_once = False
                    self.connected = False
                    raise ConnectionError("Cannot send requests while disconnected")
                yield SimpleNamespace(id=message_id)

    client = Client()
    ids = [m.id async for m in crawler._iter_messages_with_reconnect(client, "source", "source")]

    assert ids == [303, 302, 301]
    assert client.offsets == [None, 303]
    assert client.connect_calls == 1
    assert backoff_delays == [1]


@pytest.mark.asyncio
async def test_reconnect_failure_uses_three_exponential_backoffs(backoff_delays):
    class Client:
        connect_calls = 0

        def is_connected(self):
            return False

        async def connect(self):
            self.connect_calls += 1
            raise ConnectionError("proxy unreachable")

        async def get_input_entity(self, _channel):
            raise AssertionError("must not query Telegram while disconnected")

    client = Client()
    with pytest.raises(ConnectionError, match="after 3 attempts"):
        await crawler._get_input_entity_with_reconnect(client, "source")

    assert client.connect_calls == 3
    assert backoff_delays == [1, 2, 4]


@pytest.mark.asyncio
async def test_repeated_stream_errors_have_bounded_retries_even_if_client_reports_connected(backoff_delays):
    class Client:
        connect_calls = 0
        fetch_calls = 0

        def is_connected(self):
            return True

        async def connect(self):
            self.connect_calls += 1

        async def iter_messages(self, _entity):
            self.fetch_calls += 1
            raise ConnectionError("disconnected sender")
            yield  # pragma: no cover

    client = Client()
    with pytest.raises(ConnectionError, match="after 3 reconnect attempts"):
        async for _ in crawler._iter_messages_with_reconnect(client, "source", "source"):
            pass

    assert client.fetch_calls == 4  # initial request and three retries
    assert client.connect_calls == 0  # no redundant connect call while connected
    assert backoff_delays == [1, 2, 4]


@pytest.mark.asyncio
async def test_unrelated_telegram_error_is_not_retried(backoff_delays):
    class Client:
        def is_connected(self):
            return True

        async def iter_messages(self, _entity):
            raise ValueError("unsupported entity")
            yield  # pragma: no cover

    with pytest.raises(ValueError, match="unsupported entity"):
        async for _ in crawler._iter_messages_with_reconnect(Client(), "source", "source"):
            pass

    assert backoff_delays == []


@pytest.mark.asyncio
async def test_cancellation_during_backoff_is_not_swallowed(monkeypatch):
    class Client:
        def is_connected(self):
            return False

        async def connect(self):
            raise AssertionError("cancelled before reconnecting")

    async def cancelled_sleep(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(crawler.asyncio, "sleep", cancelled_sleep)
    with pytest.raises(asyncio.CancelledError):
        async for _ in crawler._iter_messages_with_reconnect(Client(), "source", "source"):
            pass
