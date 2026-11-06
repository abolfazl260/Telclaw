"""Default startup behavior across the normal runtime and authenticated Back Office."""
import asyncio
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import dotenv
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import config
import backoffice_web
from backoffice_layout import page_header
from backoffice_message_manager import MM_KEY, install


def default_configuration(monkeypatch):
    monkeypatch.delenv('TELCLAW_MESSAGE_MANAGER_ENABLED', raising=False)
    # Exercise the built-in default independently of an operator's local .env.
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda: None)
    defaults = runpy.run_path(config.__file__)
    monkeypatch.setattr(config, 'MESSAGE_MANAGER_ENABLED', defaults['MESSAGE_MANAGER_ENABLED'])


@pytest.mark.asyncio
async def test_default_backoffice_has_message_manager_without_feature_selection(mm, monkeypatch):
    default_configuration(monkeypatch)
    monkeypatch.setattr(backoffice_web, '_session', lambda request: {'csrf': 'test-token', 'admin_id': 42})
    app = web.Application(middlewares=[backoffice_web._security])
    install(app, mm)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/message-manager')
        assert response.status == 200
        assert 'Message Manager' in await response.text()
        assert app[MM_KEY] is mm
    assert 'href="/message-manager"' in page_header('publishing', 'Publishing')
    with mm.repo.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM mm_schedules').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM mm_deliveries').fetchone()[0] == 0
    assert not mm.sender.sent


@pytest.mark.asyncio
async def test_default_worker_starts_and_stops_without_backoffice(mm, monkeypatch):
    import main
    import system_ui
    import backoffice_settings
    import message_manager.runtime
    import monitoring.telegram_monitor
    import monitoring.transfer_live

    default_configuration(monkeypatch)
    monkeypatch.setattr(config, 'BACKOFFICE_ENABLED', False)
    monkeypatch.setattr(config, 'TRANSFER_TELEGRAM_PUBLISH_ENABLED', False)
    monkeypatch.setattr(backoffice_settings, 'apply_persisted_overrides', lambda: None)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def worker_run():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def ui_run():
        await asyncio.wait_for(started.wait(), timeout=2)

    runtime = SimpleNamespace(worker=SimpleNamespace(run=worker_run))
    build_runtime = Mock(return_value=runtime)
    monkeypatch.setattr(message_manager.runtime, 'build_runtime', build_runtime)
    monkeypatch.setattr(system_ui, 'SystemConsoleUI', lambda: SimpleNamespace(run=ui_run))
    monitor = SimpleNamespace(enabled=True, start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(monitoring.telegram_monitor, 'get_telegram_monitor', lambda: monitor)
    monkeypatch.setattr(monitoring.transfer_live, 'install_transfer_live_command', lambda monitor: None)

    await asyncio.wait_for(main._run(), timeout=5)

    build_runtime.assert_called_once_with()
    assert started.is_set() and stopped.is_set()
    monitor.start.assert_awaited_once()
    monitor.stop.assert_awaited_once()
