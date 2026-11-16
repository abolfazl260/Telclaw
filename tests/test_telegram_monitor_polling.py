import asyncio
import logging

import pytest

from monitoring import telegram_monitor


@pytest.mark.asyncio
async def test_polling_transport_failures_use_exponential_backoff(monkeypatch, caplog):
    monitor = telegram_monitor.TelegramMonitor()
    calls = 0
    delays = []

    async def fake_api(method, payload=None):
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise asyncio.TimeoutError("poll timed out")
        monitor._stopping.set()
        return {"ok": True, "result": []}

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(monitor, "_api", fake_api)
    monkeypatch.setattr(telegram_monitor.asyncio, "sleep", fake_sleep)
    caplog.set_level(logging.WARNING, logger=telegram_monitor.__name__)

    await monitor._poll_updates()

    assert delays == [5, 10, 20, 40]
    assert not [
        record for record in caplog.records
        if record.name == telegram_monitor.__name__
        and record.levelno >= logging.ERROR
    ]


@pytest.mark.asyncio
async def test_polling_escalates_one_sustained_outage_and_logs_recovery(
    monkeypatch, caplog
):
    monitor = telegram_monitor.TelegramMonitor()
    calls = 0

    async def fake_api(method, payload=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError("poll timed out")
        monitor._stopping.set()
        return {"ok": True, "result": []}

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(monitor, "_api", fake_api)
    monkeypatch.setattr(telegram_monitor.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        telegram_monitor, "POLL_OUTAGE_ALERT_AFTER_SECONDS", 0
    )
    caplog.set_level(logging.INFO, logger=telegram_monitor.__name__)

    await monitor._poll_updates()

    alerts = [
        record for record in caplog.records
        if record.name == telegram_monitor.__name__
        and record.levelno >= logging.ERROR
        and "polling unavailable" in record.getMessage()
    ]
    recoveries = [
        record for record in caplog.records
        if record.name == telegram_monitor.__name__
        and "polling recovered" in record.getMessage()
    ]
    assert len(alerts) == 1
    assert len(recoveries) == 1


def test_telegram_api_errors_expose_retry_after_and_retryability():
    error = telegram_monitor._telegram_api_failure(
        "getUpdates",
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 12},
        },
    )

    assert isinstance(error, telegram_monitor.TelegramAPIError)
    assert error.retry_after == 12
    assert telegram_monitor._is_retryable_poll_error(error)
    assert "HTTP 429" in str(error)
