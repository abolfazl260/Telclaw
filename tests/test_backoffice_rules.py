import sqlite3

import pytest
from aiohttp import web

import backoffice_web
import routing_rules
import routed_publisher


@pytest.fixture
def rule_db(tmp_path, monkeypatch):
    path = tmp_path / "rules.db"

    def connection():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(routing_rules, "get_connection", connection)
    monkeypatch.setattr(backoffice_web, "get_connection", connection)
    conn = connection()
    conn.executescript("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY, ai_category TEXT, ai_status TEXT,
        message_id INTEGER, sender_username TEXT);
        CREATE TABLE transferlist (id INTEGER PRIMARY KEY,
        processed_message_id INTEGER UNIQUE, origin_city TEXT,
        destination_city TEXT, origin_country TEXT, destination_country TEXT, price REAL);
        CREATE TABLE housinglist (id INTEGER PRIMARY KEY,processed_message_id INTEGER UNIQUE);
        CREATE TABLE joblist (id INTEGER PRIMARY KEY,processed_message_id INTEGER UNIQUE);
        INSERT INTO messages VALUES(1,'transferlist','processed',11,'alice');
        INSERT INTO transferlist VALUES(1,1,'Istanbul','Tehran','TR','IR',150);""")
    conn.commit()
    conn.close()
    return connection


def test_turkey_rule_routes_and_tracks_each_destination(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_target("Other", "@otherchannel")
    routing_rules.save_rule("Turkey route", "transferlist", "TR", "either", 1, priority=1)
    routing_rules.save_rule("Fallback", "transferlist", "", "either", 2, priority=10)
    pairs = routing_rules.pending()
    assert len(pairs) == 1
    assert pairs[0][1]["chat_id"] == "@turkeychannel"

    routing_rules.record_delivery(1, 1, "sent", 900)
    assert routing_rules.pending() == []

    routing_rules.save_rule("Turkey route", "transferlist", "TR", "either", 1,
                            priority=1, stop_on_match=False, rule_id=1)
    assert [rule["chat_id"] for _, rule in routing_rules.pending()] == ["@otherchannel"]


def test_destination_country_and_no_match(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("Destination Turkey", "transferlist", "TR", "destination", 1)
    assert routing_rules.pending() == []
    conn = rule_db()
    conn.execute("UPDATE transferlist SET destination_country='TR',origin_country='IR'")
    conn.commit()
    conn.close()
    assert len(routing_rules.pending()) == 1


def test_multiple_conditions_must_all_match(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("Filtered", "transferlist", "TR", "origin", 1,
                            source_channel="test", origin_city="istanbul",
                            destination_city="tehran", min_price=100, max_price=200)
    assert len(routing_rules.pending()) == 1
    conn = rule_db()
    conn.execute("UPDATE transferlist SET price=250")
    conn.commit()
    conn.close()
    assert routing_rules.pending() == []


@pytest.mark.asyncio
async def test_backoffice_link_is_admin_only_and_single_use(rule_db, monkeypatch):
    monkeypatch.setattr(backoffice_web.config, "BACKOFFICE_PUBLIC_URL", "https://office.example.org")
    with pytest.raises(PermissionError):
        backoffice_web.issue_link(123)
    url = backoffice_web.issue_link(1485409432)
    token = url.split("token=", 1)[1]
    assert token not in str(rule_db().execute("SELECT token_hash FROM backoffice_links").fetchone()[0])

    class Request:
        query = {"token": token}

    with pytest.raises(web.HTTPSeeOther) as first:
        await backoffice_web.login(Request())
    assert "telclaw_admin" in first.value.cookies
    assert first.value.cookies["telclaw_admin"]["httponly"]
    assert first.value.cookies["telclaw_admin"]["secure"]
    with pytest.raises(web.HTTPForbidden):
        await backoffice_web.login(Request())


@pytest.mark.asyncio
async def test_direct_http_ip_link_allows_new_admin_login(rule_db, monkeypatch):
    config = backoffice_web.config
    monkeypatch.setattr(config, "BACKOFFICE_PUBLIC_URL", "http://192.0.2.10:8787")
    monkeypatch.setattr(config, "BACKOFFICE_PUBLIC_PORT", 0)
    monkeypatch.setattr(config, "BACKOFFICE_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "BACKOFFICE_PORT", 8787)
    monkeypatch.setattr(config, "BACKOFFICE_TLS_CERT", "")
    monkeypatch.setattr(config, "BACKOFFICE_TLS_KEY", "")
    url = backoffice_web.issue_link(106056586)
    assert url.startswith("http://192.0.2.10:8787/login?token=")

    class Request:
        query = {"token": url.split("token=", 1)[1]}

    with pytest.raises(web.HTTPSeeOther) as result:
        await backoffice_web.login(Request())
    assert "telclaw_admin" in result.value.cookies
    assert not result.value.cookies["telclaw_admin"]["secure"]


@pytest.mark.asyncio
async def test_publisher_sends_only_once_per_destination(rule_db, monkeypatch):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("Turkey", "transferlist", "TR", "either", 1)
    monkeypatch.setattr(routed_publisher.config, "TELEGRAM_BOT_TOKEN", "fake-token")
    posted = []

    class Response:
        ok = True
        status = 200

        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs):
            return {"ok": True, "result": {"message_id": 900}}

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            posted.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(routed_publisher.aiohttp, "ClientSession", Session)
    publisher = routed_publisher.RoutedPublisher()
    assert (await publisher.publish_pending())["sent"] == 1
    assert (await publisher.publish_pending())["sent"] == 0
    assert [item["chat_id"] for item in posted] == ["@turkeychannel"]
