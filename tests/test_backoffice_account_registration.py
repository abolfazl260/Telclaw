from types import SimpleNamespace

import pytest

import config
import sessions_manager


class FakeSentCode:
    phone_code_hash = "hash"


class FakeClient:
    def __init__(self):
        self.connected = False
        self.authorized = False
        self.code_calls = []
        self.sign_in_calls = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.code_calls.append(phone)
        return FakeSentCode()

    async def sign_in(self, **kwargs):
        self.sign_in_calls.append(kwargs)
        self.authorized = True

    async def get_me(self):
        return SimpleNamespace(id=123, username="tester", phone="15550000000")


@pytest.mark.asyncio
async def test_noninteractive_account_registration_completes_without_terminal_input(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_DIR", str(tmp_path))
    sessions_manager._registration_cache.clear()
    sessions_manager._client_cache.clear()
    client = FakeClient()
    monkeypatch.setattr(sessions_manager, "create_client", lambda _name: client)

    started = await sessions_manager.begin_account_registration("web-account", "+15550000000")
    assert started == {"session": "web-account", "stage": "code"}
    assert sessions_manager.get_account_registration_state("web-account") == {
        "session": "web-account",
        "stage": "code",
    }

    completed = await sessions_manager.submit_account_registration_code("web-account", "12345")
    assert completed == {"session": "web-account", "stage": "complete"}
    assert client.sign_in_calls == [{
        "phone": "+15550000000",
        "code": "12345",
        "phone_code_hash": "hash",
    }]
    assert sessions_manager.get_account_registration_state("web-account") is None
    assert (tmp_path / "web-account.meta.json").exists()


@pytest.mark.asyncio
async def test_account_registration_can_be_cancelled_and_cleans_pending_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_DIR", str(tmp_path))
    sessions_manager._registration_cache.clear()
    sessions_manager._client_cache.clear()
    client = FakeClient()
    monkeypatch.setattr(sessions_manager, "create_client", lambda _name: client)

    await sessions_manager.begin_account_registration("cancel-me", "+15550000000")
    assert await sessions_manager.cancel_account_registration("cancel-me") is True
    assert sessions_manager.get_account_registration_state("cancel-me") is None
    assert client.connected is False
