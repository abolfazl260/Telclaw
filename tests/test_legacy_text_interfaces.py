"""Protect legacy CSV and incoming Telegram payload 'text' interfaces.

SQLite messages.text is NOT interchangeable with these unrelated keys.
Never use repository-wide find/replace of the identifier "text".
No pandas requirement is introduced into core Telclaw CI and no real bot runs.
"""

import ast
import importlib.util
from pathlib import Path

import pytest

from monitoring.telegram_monitor import TelegramMonitor
from monitoring import transfer_live
from processing.cleaner import clean_text


ROOT = Path(__file__).resolve().parents[1]


def _literal_subscript_key(node):
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value
    return None


def test_legacy_csv_cleaner_keeps_its_text_and_derived_column_contract():
    cleaner_path = ROOT / "TelegramProcessor" / "modules" / "cleaner.py"
    spec = importlib.util.spec_from_file_location(
        "legacy_csv_cleaner_contract", cleaner_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    original = " Hello  😀  World \n"
    assert module.normalize_text(original) == "hello world"
    # Main SQLite processing has a different contract: no emoji removal
    # and preservation of original case.
    assert clean_text(original) == "Hello 😀 World"

    tree = ast.parse(cleaner_path.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert {"remove_emojis", "normalize_text", "clean_dataframe"}.issubset(functions)

    clean_func = functions["clean_dataframe"]
    assigned_columns = set()
    read_columns = set()
    for node in ast.walk(clean_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    if target.value.id == "df":
                        assigned_columns.add(_literal_subscript_key(target))
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == "df":
                read_columns.add(_literal_subscript_key(node))
    assert {"original_text", "normalized_text"}.issubset(assigned_columns)
    assert "text" in read_columns


def test_legacy_csv_export_still_maps_output_text_to_row_text():
    # TelegramProcessor/main.py imports pandas (not required by current
    # Telclaw production requirements), so validate its actual export AST
    # without importing or launching the legacy batch executable.
    tree = ast.parse(
        (ROOT / "TelegramProcessor" / "main.py").read_text(encoding="utf-8")
    )
    found_mapping = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or key.value != "text":
                continue
            if (
                isinstance(value, ast.Subscript)
                and isinstance(value.value, ast.Name)
                and value.value.id == "row"
                and _literal_subscript_key(value) == "text"
            ):
                found_mapping = True
    assert found_mapping, "The legacy CSV exporter must retain text=row['text']"


@pytest.mark.asyncio
async def test_monitor_status_handler_uses_incoming_message_text_not_db_column(
    monkeypatch,
):
    monitor = TelegramMonitor()
    received = []

    async def fake_send(chat_id, message, reply_markup=None):
        received.append((chat_id, message, reply_markup))

    async def fake_status():
        return "Safe local status response"

    monkeypatch.setattr(monitor, "_send", fake_send)
    monkeypatch.setattr(monitor, "_build_status_message", fake_status)
    monkeypatch.setattr(monitor, "_is_subscribed", lambda _chat_id: True)
    admin_id = 1485409432
    await monitor._handle_update({
        "message": {
            "chat": {"id": admin_id, "type": "private"},
            "from": {"id": admin_id},
            "text": "  /status@TelclawBot  ",
        }
    })
    assert len(received) == 1
    assert received[0][0] == admin_id
    assert received[0][1] == "Safe local status response"
    assert "inline_keyboard" in received[0][2]


@pytest.mark.asyncio
async def test_transferlive_monitor_extension_preserves_incoming_text_routing(
    monkeypatch,
):
    class LocalMonitor:
        def __init__(self):
            self.delegated = []
            self.delivered = []

        async def _handle_update(self, update):
            self.delegated.append(update)

        async def _register_commands(self):
            return None

        def _is_admin_private_chat(self, chat, user):
            return chat.get("type") == "private" and user.get("id") == chat.get("id")

        def _is_subscribed(self, chat_id):
            return chat_id == 1485409432

        async def _send(self, chat_id, text):
            self.delivered.append((chat_id, text))

    async def fake_send_transferlive(monitor, chat_id):
        monitor.delivered.append((chat_id, "transferlive fixture"))

    monkeypatch.setattr(transfer_live, "_send_transfer_live", fake_send_transferlive)

    monitor = LocalMonitor()
    assert transfer_live.install_transfer_live_command(monitor) is monitor
    admin_id = 1485409432

    def incoming(text):
        return {
            "message": {
                "chat": {"id": admin_id, "type": "private"},
                "from": {"id": admin_id},
                "text": text,
            }
        }

    await monitor._handle_update(incoming(" /transferlive@TelclawBot  "))
    assert monitor.delivered == [(admin_id, "transferlive fixture")]
    assert monitor.delegated == []

    unrelated = incoming("/status")
    await monitor._handle_update(unrelated)
    assert monitor.delegated == [unrelated]

    # Empty string must route through the original handler unchanged.
    empty = incoming("")
    await monitor._handle_update(empty)
    assert monitor.delegated == [unrelated, empty]
