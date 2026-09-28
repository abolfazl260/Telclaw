import sqlite3

import pytest
from aiohttp import web

import backoffice_web
import routing_rules


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
        destination_city TEXT, origin_country TEXT, destination_country TEXT);
        CREATE TABLE housinglist (id INTEGER PRIMARY KEY,processed_message_id INTEGER UNIQUE);
        CREATE TABLE joblist (id INTEGER PRIMARY KEY,processed_message_id INTEGER UNIQUE);
        INSERT INTO messages VALUES(1,'transferlist','processed',11,'alice');
        INSERT INTO transferlist VALUES(1,1,'Istanbul','Tehran','TR','IR');""")
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
