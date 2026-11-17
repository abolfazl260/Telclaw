"""Demand-based top-7 cargo origins: filtering, ranking, rendering, and access."""
from datetime import datetime, timedelta, timezone
import pytest

import config
from reports import cargo_demand_origins as demand
from storage import database

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def demand_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "demand.sqlite"))
    database.initialize_db()

    def insert(message_id, *, sender_id=None, role="shipper", origin="Tehran",
               country="IR", destination="Toronto", dest_country="CA",
               departure="2026-11-30", days=2, posted=None, status="processed",
               processing="processed", category="transferlist", channel="cargo"):
        when = posted or (NOW - timedelta(days=days)).isoformat()
        with database.get_connection() as conn:
            conn.execute(
                """INSERT INTO messages
                   (channel_username, message_id, date, processing_status, ai_status,
                    ai_category, sender_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (channel, message_id, when, processing, status, category,
                 message_id if sender_id is None else sender_id),
            )
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO transferlist
                   (processed_message_id, origin_city, origin_country,
                    destination_city, destination_country, transfer_role, departure_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (row_id, origin, country, destination, dest_country, role, departure),
            )
            return row_id
    return insert


def test_only_shipper_requests_with_valid_origins_count(demand_db):
    demand_db(1, origin="Tehran")
    demand_db(2, origin="Tehran", role="passenger")
    demand_db(3, origin="Tehran", role=None)
    demand_db(4, origin="Tehran", status="failed")
    demand_db(5, origin="Tehran", processing="failed")
    demand_db(6, origin="Tehran", category="housinglist")
    demand_db(7, origin=None)
    demand_db(8, country="??")
    demand_db(9, origin="Dubai", country="AE")
    demand_db(10, origin="Tehran", country="CA")
    report = demand.build_demand_report(NOW)
    assert report["total"] == 4
    assert report["origin_count"] == 3
    assert [(r["city"], r["country"], r["count"]) for r in report["top"]] == [
        ("Tehran", "IR", 2), ("Dubai", "AE", 1), ("Tehran", "CA", 1)
    ]


def test_deduplicates_same_trip_across_channels_and_keeps_distinct_routes(demand_db):
    demand_db(1, sender_id=42, channel="first")
    demand_db(2, sender_id=42, channel="second")
    demand_db(3, sender_id=42, destination="Vancouver")
    demand_db(4, sender_id=99)
    demand_db(5, sender_id=42, destination=None)
    demand_db(6, sender_id=42, destination=None)
    report = demand.build_demand_report(NOW)
    assert report["total"] == 5
    assert report["top"][0]["count"] == 5


def test_periods_are_rolling_and_exclude_future_and_unprocessed(demand_db):
    demand_db(1, posted=(NOW - timedelta(days=7)).isoformat())
    demand_db(2, posted=(NOW - timedelta(days=7, seconds=1)).isoformat())
    demand_db(3, posted=(NOW - timedelta(days=15)).isoformat())
    demand_db(4, posted=(NOW - timedelta(days=30)).isoformat())
    demand_db(5, posted=(NOW - timedelta(days=30, seconds=1)).isoformat())
    demand_db(6, posted=(NOW + timedelta(seconds=1)).isoformat())
    assert demand.build_demand_report(NOW, days=7)["total"] == 1
    assert demand.build_demand_report(NOW, days=15)["total"] == 3
    assert demand.build_demand_report(NOW, days=30)["total"] == 4
    with pytest.raises(ValueError, match="Unsupported"):
        demand.build_demand_report(NOW, days=31)
    with pytest.raises(ValueError, match="timezone"):
        demand.build_demand_report(datetime(2026, 11, 17), days=7)


def test_top_seven_is_sorted_and_format_is_safe_html(demand_db):
    cities = ["<Paris&>", "Moscow", "Berlin", "Dubai", "Tehran",
              "London", "Toronto", "Rome", "Madrid"]
    for index, city in enumerate(cities, 1):
        for offset in range(10 - index):
            demand_db(index * 100 + offset, origin=city, country="FR" if index == 1 else "IR")
    report = demand.build_demand_report(NOW)
    assert report["total"] == sum(range(1, 10))
    assert len(report["top"]) == 7
    assert report["top"][0]["count"] == 9
    message = demand.format_demand_report(report)
    assert "&lt;Paris&amp;&gt;" in message
    assert "<Paris&>" not in message
    assert "▰▰▰" in message
    assert "۳۰ روز اخیر" in message
    assert "<code>/cargoorigins 7</code>" in message
    assert "shipper" in message
    assert len(message) < 4096


def test_origin_cache_canonical_name_is_used_if_available(demand_db):
    first = demand_db(1, origin="Москва", country="RU", destination="Dubai", dest_country="AE")
    demand_db(2, origin="Moscow", country="RU", destination="Dubai", dest_country="AE")
    with database.get_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS transfer_locations (
            processed_message_id INTEGER PRIMARY KEY,
            origin_city_canonical TEXT, origin_city_key TEXT, origin_country_iso2 TEXT,
            destination_city_canonical TEXT, destination_city_key TEXT,
            destination_country_iso2 TEXT
        )""")
        conn.execute(
            """INSERT INTO transfer_locations (processed_message_id, origin_city_canonical,
               origin_country_iso2, destination_city_canonical, destination_country_iso2)
               VALUES (?, 'Moscow', 'RU', 'Dubai', 'AE')""", (first,)
        )
    report = demand.build_demand_report(NOW)
    assert report["origin_count"] == 1
    assert report["top"] == [{"city": "Moscow", "country": "RU", "count": 2}]


def test_empty_report_renders_helpful_message(demand_db):
    report = demand.build_demand_report(NOW)
    assert report["total"] == 0 and report["top"] == []
    text = demand.format_demand_report(report)
    assert "در این بازه درخواست ارسال بار" in text
    assert "/cargoorigins 15" in text


@pytest.mark.asyncio
async def test_report_uses_send_message_not_send_widget(demand_db):
    demand_db(1)
    calls = []

    class Monitor:
        async def _api(self, method, payload):
            calls.append((method, payload))

    await demand.send_demand_report(Monitor(), 123, now=NOW, days=15)
    assert len(calls) == 1
    method, payload = calls[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == 123
    assert payload["parse_mode"] == "HTML"
    assert "reply_markup" not in payload
    assert "۱۵ روز اخیر" in payload["text"]


@pytest.mark.asyncio
async def test_command_private_chat_only_and_supports_periods(monkeypatch):
    from monitoring.telegram_monitor import TelegramMonitor
    seen = []

    async def fake_send(_monitor, chat_id, *, days=30):
        seen.append((chat_id, days))

    monkeypatch.setattr(demand, "send_demand_report", fake_send)
    bot = TelegramMonitor()

    async def fake_api(method, payload):
        seen.append((method, payload["chat_id"]))

    monkeypatch.setattr(bot, "_api", fake_api)
    msg = {"chat": {"id": 777, "type": "private"},
           "from": {"id": 777}, "text": "/cargoorigins"}
    await bot._handle_update({"message": msg})
    msg["text"] = "/cargoorigins ۷"
    await bot._handle_update({"message": msg})
    msg["text"] = "/cargoorigins 15"
    await bot._handle_update({"message": msg})
    msg["text"] = "/cargoorigins 40"
    await bot._handle_update({"message": msg})
    assert seen == [(777, 30), (777, 7), (777, 15), ("sendMessage", 777)]
    msg["text"] = "/cargoorigins 30"
    msg["chat"] = {"id": -100, "type": "supergroup"}
    await bot._handle_update({"message": msg})
    msg["chat"] = {"id": 777, "type": "private"}
    msg["from"] = {"id": 999}
    await bot._handle_update({"message": msg})
    assert len(seen) == 4
