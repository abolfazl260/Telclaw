"""Weekly cargo report: scoped aggregation, widget constraints and delivery safety."""
import json
import zlib
from datetime import datetime, timezone

import pytest

import config
from reports import cargo_weekly
from storage import database
from ai.category_schemas import CATEGORY_FIELDS, validate_result
from ai.prompt_loader import render_prompt


@pytest.fixture
def cargo_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "cargo.sqlite"))
    database.initialize_db()

    def add(message_id, *, sender_id=17, role="passenger", origin="Tehran",
            destination="Toronto", origin_country="IR", dest_country="CA",
            posted="2026-11-03T12:00:00+00:00", status="processed", channel="a"):
        with database.get_connection() as db:
            db.execute(
                """INSERT INTO messages
                   (channel_username, message_id, date, processing_status, ai_status, ai_category, sender_id)
                   VALUES (?, ?, ?, 'processed', ?, 'transferlist', ?)""",
                (channel, message_id, posted, status, sender_id),
            )
            row_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                """INSERT INTO transferlist
                   (processed_message_id, origin_city, origin_country, destination_city,
                    destination_country, transfer_role, departure_date)
                   VALUES (?, ?, ?, ?, ?, ?, '2026-11-17')""",
                (row_id, origin, origin_country, destination, dest_country, role),
            )
    return add


NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def test_report_has_no_env_setup_and_uses_existing_advertio_cargo_destination(monkeypatch):
    monkeypatch.setenv("TELCLAW_CARGO_WEEKLY_REPORT_ENABLED", "false")
    monkeypatch.setenv("TELCLAW_CARGO_WEEKLY_REPORT_CHANNEL", "@wrong_channel")
    assert cargo_weekly.REPORT_CHANNEL == "@advertio_cargo"
    assert not hasattr(config, "CARGO_WEEKLY_REPORT_ENABLED")
    assert not hasattr(config, "CARGO_WEEKLY_REPORT_CHANNEL")


def test_week_boundaries_are_complete_tehran_weeks():
    start, end = cargo_weekly.previous_complete_week(NOW)
    assert (start.date().isoformat(), end.date().isoformat()) == ("2026-11-02", "2026-11-09")
    assert start.hour == end.hour == 0


def test_report_counts_unique_roles_not_just_messages(cargo_db):
    cargo_db(1)
    cargo_db(2, channel="b")  # same sender, route and trip: duplicate
    cargo_db(3, sender_id=23, role="shipper")
    cargo_db(4, sender_id=24, role=None)
    cargo_db(5, sender_id=25, role="shipper", dest_country="??")  # invalid country
    cargo_db(6, sender_id=26, role="shipper", posted="2026-11-10T12:00:00+00:00")
    cargo_db(7, sender_id=27, role="shipper", status="failed")
    report = cargo_weekly.build_report(NOW)
    assert (report["total"], report["route_count"], report["unknown"]) == (3, 1, 1)
    assert report["routes"][0]["total"] == 3
    assert report["routes"][0]["shipper"] == 1
    assert report["routes"][0]["passenger"] == 1
    assert report["routes"][0]["unknown"] == 1


def test_widget_payload_is_under_telegram_limits(cargo_db):
    cargo_db(1)
    report = cargo_weekly.build_report(NOW)
    payload = cargo_weekly.widget_payload(1234, report)
    assert payload["chat_id"] == 1234
    assert "TelegramWidget" in payload["html"]
    assert "http://" not in payload["html"]
    assert len(payload["html"].encode("utf-8")) <= 32768
    assert len(zlib.compress(payload["html"].encode("utf-8"), 9)) <= 4096
    data = json.loads(payload["public_data"])
    assert len(payload["public_data"].encode("utf-8")) <= 1024
    assert data["r"][0][2:] == [0, 1, 0]
    assert payload["allow_queries"] is False
    assert "height" not in payload  # Telegram picks natural height instead of clamping 0 to 50px


def test_channel_post_has_private_deep_link_and_no_widget(cargo_db):
    cargo_db(1)
    payload = cargo_weekly.channel_post(cargo_weekly.build_report(NOW), "AdvertioHelperBot")
    assert "sendWidget" not in payload
    assert "آگهی" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"] == (
        "https://t.me/AdvertioHelperBot?start=cargo_weekly"
    )


@pytest.mark.asyncio
async def test_weekly_publisher_is_idempotent(cargo_db, monkeypatch):
    cargo_db(1)
    calls = []

    class Monitor:
        enabled = True

        async def _api(self, method, payload=None):
            calls.append((method, payload))
            if method == "getMe":
                return {"result": {"username": "AdvertioHelperBot"}}
            return {"result": {"message_id": 990}}

    monitor = Monitor()
    assert await cargo_weekly.publish_weekly_once(monitor, now=NOW)
    assert not await cargo_weekly.publish_weekly_once(monitor, now=NOW)
    assert sum(method == "sendMessage" for method, _ in calls) == 1
    assert next(payload["chat_id"] for method, payload in calls if method == "sendMessage") == "@advertio_cargo"
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT status, telegram_message_id FROM cargo_weekly_report_publications"
        ).fetchone()
    assert tuple(row) == ("sent", 990)


@pytest.mark.asyncio
async def test_ambiguous_send_is_not_repeated(cargo_db, monkeypatch):
    cargo_db(1)
    sends = []

    class Monitor:
        enabled = True

        async def _api(self, method, payload=None):
            if method == "getMe":
                return {"result": {"username": "AdvertioHelperBot"}}
            sends.append(payload)
            raise TimeoutError("Telegram reply lost after a possible send")

    monitor = Monitor()
    assert not await cargo_weekly.publish_weekly_once(monitor, now=NOW)
    assert not await cargo_weekly.publish_weekly_once(monitor, now=NOW)
    assert len(sends) == 1
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT status FROM cargo_weekly_report_publications"
        ).fetchone()[0] == "uncertain"


@pytest.mark.asyncio
async def test_public_private_command_does_not_require_admin(monkeypatch):
    from monitoring.telegram_monitor import TelegramMonitor
    from reports import cargo_weekly

    seen = []

    async def fake_report(_monitor, chat_id):
        seen.append(chat_id)

    monkeypatch.setattr(cargo_weekly, "send_private_report", fake_report)
    bot = TelegramMonitor()
    message = {"chat": {"id": 424242, "type": "private"},
               "from": {"id": 424242}, "text": "/start cargo_weekly"}
    await bot._handle_update({"message": message})
    assert seen == [424242]
    message["chat"] = {"id": -10042, "type": "channel"}
    await bot._handle_update({"message": message})
    assert seen == [424242]


def test_ai_extracts_explicit_transfer_role_without_guessing():
    assert "transfer_role" in CATEGORY_FIELDS["transferlist"]
    prompt = render_prompt("transferlist", "من هفته بعد به کانادا سفر دارم و بار شما را می‌برم")
    assert '"transfer_role":null' in prompt
    assert 'transfer_role is "shipper"' in prompt
    category, fields = validate_result({
        "category": "transferlist",
        "data": {"transferlist": {"transfer_role": "passenger", "origin_city": "Tehran"}},
    })
    assert category == "transferlist"
    assert fields["transfer_role"] == "passenger"

