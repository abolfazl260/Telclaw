"""Regression tests for Q navigation, cancellable input and per-item stop."""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

import terminal_input
from terminal_input import ConsoleBack
from ui import ConsoleUI
from system_ui import SystemConsoleUI
from delivery import telegram_transfer
from delivery.advertio_service import AdvertioDeliveryService
import sessions_manager


@pytest.mark.asyncio
async def test_q_aborts_menu_and_text_prompts(monkeypatch):
    async def read_q(_prompt=""):
        return " Q "

    monkeypatch.setattr("ui.read_line", read_q)
    console = ConsoleUI.__new__(ConsoleUI)
    with pytest.raises(ConsoleBack):
        await console.prompt_choice("Choice: ", {"1", "2"})
    with pytest.raises(ConsoleBack):
        await console.prompt_text("Account name", default="main", allow_empty=False)
    assert await console.pause() is True


@pytest.mark.asyncio
async def test_q_cancels_telegram_registration_prompt(monkeypatch):
    async def read_q(_prompt=""):
        return "q"

    monkeypatch.setattr(sessions_manager, "read_line", read_q)
    with pytest.raises(ConsoleBack):
        await sessions_manager._prompt("Phone: ")


@pytest.mark.asyncio
async def test_cancelled_input_waiter_does_not_steal_next_line(monkeypatch):
    # Avoid starting the real stdin reader in this unit test.
    monkeypatch.setattr(terminal_input, "_lines", deque())
    monkeypatch.setattr(terminal_input, "_waiter", None)
    monkeypatch.setattr(terminal_input, "_end_of_input", False)
    monkeypatch.setattr(terminal_input, "_reader_thread", object())

    waiter = asyncio.create_task(terminal_input.read_line())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    with terminal_input._lock:
        terminal_input._lines.append("2")
    assert await terminal_input.read_line() == "2"


@pytest.mark.asyncio
async def test_active_operation_stops_with_q(monkeypatch):
    input_lines = asyncio.Queue()

    async def read_line(_prompt=""):
        return await input_lines.get()

    monkeypatch.setattr("system_ui.read_line", read_line)
    console = SimpleNamespace(show_message=lambda *_args: None)

    async def operation(should_stop):
        await input_lines.put("q")
        # Allow the Q listener to run before the operation checks the flag.
        await asyncio.sleep(0)
        return should_stop()

    assert await SystemConsoleUI._run_with_q_stop(console, operation) is True


@pytest.mark.asyncio
async def test_transfer_delivery_stops_before_next_ad(monkeypatch):
    records = [{"processed_message_id": 1}, {"processed_message_id": 2}]
    sent = []

    class Client:
        async def send_message(self, _channel, text):
            sent.append(text)
            return SimpleNamespace(id=len(sent))

    monkeypatch.setattr(telegram_transfer, "get_ready_transfer_ads", lambda limit: records)
    monkeypatch.setattr(telegram_transfer, "format_transfer_ad", lambda record: str(record["processed_message_id"]))
    monkeypatch.setattr(telegram_transfer, "record_transfer_delivery", lambda *_args, **_kwargs: None)

    result = await telegram_transfer.send_transfer_ads(
        Client(), "@destination", limit=2, should_stop=lambda: len(sent) == 1
    )
    assert sent == ["1"]
    assert result["sent"] == 1
    assert result["stopped"] is True


def test_advertio_delivery_stops_before_next_listing():
    records = [{"message_id": 1, "housing_data": {}}, {"message_id": 2, "housing_data": {}}]
    sent = []
    service = AdvertioDeliveryService.__new__(AdvertioDeliveryService)
    service.repository = SimpleNamespace(get_advertio_pending=lambda **_kwargs: records)
    service.prepare_media_for_delivery = lambda *_args, **_kwargs: None
    service.deliver = lambda record, _data: sent.append(record["message_id"])
    service.finalize_successful_delivery = lambda *_args, **_kwargs: "sent"

    result = service.deliver_pending(limit=2, progress=False, should_stop=lambda: len(sent) == 1)
    assert sent == [1]
    assert result["sent"] == 1
    assert result["stopped"] is True
