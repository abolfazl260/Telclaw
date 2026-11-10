"""Origin-country interactive widget: time windows, counts, Telegram constraints."""
import json
import zlib
from datetime import datetime, timedelta, timezone

import pytest

import config
from reports import cargo_countries
from storage import database


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def country_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "countries.sqlite"))
    database.initialize_db()

    def insert(message_id, *, country="RU", days=1, sender_id=None, role="passenger",
               origin="Moscow", destination="Dubai", dest_country="AE",
               departure="2026-11-20", status="processed", process="processed",
               channel="cargo", city_missing=False, posted=None):
        when = posted or (NOW - timedelta(days=days)).isoformat()
        with database.get_connection() as conn:
            conn.execute(
                """INSERT INTO messages
                   (channel_username, message_id, date, processing_status, ai_status,
                    ai_category, sender_id)
                   VALUES (?, ?, ?, ?, ?, 'transferlist', ?)""",
                (channel, message_id, when, process, status,
                 message_id if sender_id is None else sender_id),
            )
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO transferlist
                   (processed_message_id, origin_city, origin_country, destination_city,
                    destination_country, transfer_role, departure_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (row_id, None if city_missing else origin, country, destination,
                 dest_country, role, departure),
            )
    return insert


def test_rolling_7_15_30_day_counts_and_country_grouping(country_db):
    country_db(1, country="RU", days=1)
    country_db(2, country="RU", days=9)
    country_db(3, country="IR", days=14)
    country_db(4, country="CA", days=29)
    country_db(5, country="US", days=31)  # outside all windows
    country_db(6, country="??", days=2)  # invalid origin
    country_db(7, country="DE", days=2, status="failed")
    country_db(8, country="RU", days=2, sender_id=1)  # same trip repost
    country_db(9, country="RU", days=1, sender_id=99)
    country_db(10, country="IR", days=4, sender_id=100)
    result = cargo_countries.build_country_snapshot(NOW)
    assert result["as_of"] == "2026-11-10 15:30"  # Tehran
    assert result["totals"] == [3, 5, 6]
    assert result["country_counts"] == [2, 2, 3]
    by_code = {r[0]: r[1:] for r in result["countries"]}
    assert by_code["RU"] == [2, 3, 3]
    assert by_code["IR"] == [1, 2, 2]
    assert by_code["CA"] == [0, 0, 1]
    assert "US" not in by_code


def test_start_boundary_inclusive_and_future_excluded(country_db):
    country_db(1, country="RU", posted=(NOW - timedelta(days=7)).isoformat())
    country_db(2, country="IR", posted=(NOW - timedelta(days=7, seconds=1)).isoformat())
    country_db(3, country="TR", posted=(NOW + timedelta(seconds=1)).isoformat())
    snapshot = cargo_countries.build_country_snapshot(NOW)
    assert snapshot["totals"] == [1, 2, 2]


def test_messages_without_complete_route_still_count_if_origin_is_valid(country_db):
    country_db(1, country="IR", days=2, city_missing=True)
    country_db(2, country="IR", days=2, city_missing=True)
    country_db(3, country="ZZ", days=1)
    country_db(4, country=None, days=1)
    snapshot = cargo_countries.build_country_snapshot(NOW)
    assert snapshot["totals"] == [2, 2, 2]
    assert snapshot["countries"] == [["IR", 2, 2, 2]]


def test_widget_contains_local_period_tabs_and_is_within_bot_api_limits(country_db):
    country_db(1, country="IR")
    snapshot = cargo_countries.build_country_snapshot(NOW)
    payload = cargo_countries.country_widget_payload(42, snapshot)
    assert payload["chat_id"] == 42
    assert payload["allow_queries"] is False
    assert "reply_markup" not in payload
    assert "height" not in payload  # natural height
    assert "id=\"p7\"" in payload["html"]
    assert "id=\"p15\"" in payload["html"]
    assert "id=\"p30\"" in payload["html"]
    assert "addEventListener('click'" in payload["html"]
    assert "https://" not in payload["html"]
    assert "http://" not in payload["html"]
    html = payload["html"].encode("utf-8")
    assert len(html) <= 32768
    assert len(zlib.compress(html, 9)) <= 4096
    state = payload["public_data"]
    assert len(state.encode("utf-8")) <= 1024
    assert json.loads(state)["t"] == [1, 1, 1]
    assert json.loads(state)["c"] == [["IR", 1, 1, 1]]


def test_candidate_countries_cover_top_eight_for_each_period_and_keep_totals(country_db):
    # 12 valid ISO-like codes exceed the 8 bars shown by the widget.
    for i, code in enumerate(["IR", "RU", "AE", "CA", "US", "DE", "TR",
                              "CN", "GB", "FR", "IT", "ES"], start=1):
        country_db(i, country=code, sender_id=i)
    snapshot = cargo_countries.build_country_snapshot(NOW)
    assert snapshot["totals"] == [12, 12, 12]
    assert snapshot["country_counts"] == [12, 12, 12]
    assert len(snapshot["countries"]) == 8
    assert sum(row[1] for row in snapshot["countries"]) == 8
    assert len(cargo_countries.country_widget_payload(42, snapshot)["public_data"].encode("utf-8")) <= 1024


@pytest.mark.asyncio
async def test_country_command_sends_widget_without_extra_telegram_calls(country_db):
    country_db(1, country="IR")
    calls = []

    class Monitor:
        async def _api(self, method, payload=None):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 17}}

    await cargo_countries.send_country_report(Monitor(), 123, now=NOW)
    assert len(calls) == 1
    assert calls[0][0] == "sendWidget"
    assert calls[0][1]["chat_id"] == 123


@pytest.mark.asyncio
async def test_empty_country_report_still_has_interactive_period_selector(country_db):
    calls = []

    class Monitor:
        async def _api(self, method, payload=None):
            calls.append((method, payload))

    await cargo_countries.send_country_report(Monitor(), 123, now=NOW)
    assert calls[0][0] == "sendWidget"
    assert json.loads(calls[0][1]["public_data"])["t"] == [0, 0, 0]


@pytest.mark.asyncio
async def test_cargo_countries_is_public_only_in_own_private_chat(monkeypatch):
    from monitoring.telegram_monitor import TelegramMonitor
    seen = []

    async def fake_send(_monitor, chat_id):
        seen.append(chat_id)

    monkeypatch.setattr(cargo_countries, "send_country_report", fake_send)
    bot = TelegramMonitor()
    msg = {
        "chat": {"id": 777, "type": "private"},
        "from": {"id": 777},
        "text": "/cargocountries",
    }
    await bot._handle_update({"message": msg})
    assert seen == [777]
    msg["chat"] = {"id": -100, "type": "supergroup"}
    await bot._handle_update({"message": msg})
    assert seen == [777]
    msg["chat"] = {"id": 777, "type": "private"}
    msg["from"] = {"id": 888}
    await bot._handle_update({"message": msg})
    assert seen == [777]
