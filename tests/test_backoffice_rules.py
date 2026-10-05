import sqlite3

import pytest
from aiohttp import web

import backoffice_web
import routing_rules
import routed_publisher
from delivery import telegram_transfer_publisher


@pytest.fixture
def rule_db(tmp_path, monkeypatch):
    path = tmp_path / "rules.db"

    def connection():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(routing_rules, "get_connection", connection)
    monkeypatch.setattr(backoffice_web, "get_connection", connection)
    monkeypatch.setattr(routed_publisher, "get_connection", connection)
    monkeypatch.setattr(telegram_transfer_publisher.database, "get_connection", connection)
    conn = connection()
    conn.executescript("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY, ai_category TEXT, ai_status TEXT,
        processing_status TEXT, message_id INTEGER, sender_username TEXT,
        channel_username TEXT, message_link TEXT, raw_text TEXT, text TEXT);
        CREATE TABLE transferlist (id INTEGER PRIMARY KEY,
        processed_message_id INTEGER UNIQUE, origin_city TEXT,
        destination_city TEXT, origin_country TEXT, destination_country TEXT,
        price REAL, departure_date TEXT);
        CREATE TABLE housinglist (id INTEGER PRIMARY KEY,processed_message_id INTEGER UNIQUE);
        CREATE TABLE joblist (id INTEGER PRIMARY KEY,processed_message_id INTEGER UNIQUE);
        INSERT INTO messages VALUES(1,'transferlist','processed','processed',11,'alice','test','','original user text','legacy source text');
        INSERT INTO transferlist VALUES(1,1,'Istanbul','Tehran','TR','IR',150,'2020-01-01');""")
    conn.commit()
    conn.close()
    return connection


def test_publisher_migrates_legacy_koolbar_target_in_place(rule_db):
    routing_rules.save_target("Koolbar International", "@koolbar_international")
    routing_rules.record_delivery(1, 1, "sent", telegram_message_id=900)

    routed_publisher.RoutedPublisher(token="fake-token")

    targets = routing_rules.list_targets()
    assert len(targets) == 1
    assert targets[0]["id"] == 1
    assert targets[0]["label"] == "Advertio Cargo"
    assert targets[0]["chat_id"] == "@advertio_cargo"

    delivery = routing_rules.recent_deliveries(target_id=1)[0]
    assert delivery["status"] == "sent"
    assert delivery["telegram_message_id"] == 900


def test_legacy_koolbar_skips_incomplete_transfer_routes(rule_db):
    routing_rules.save_target("Advertio Cargo", "@advertio_cargo")
    conn = rule_db()
    conn.execute("UPDATE transferlist SET departure_date='2099-01-01', origin_city=NULL WHERE id=1")
    conn.commit()
    conn.close()

    assert routed_publisher.RoutedPublisher._koolbar_pairs(limit=10) == []


def test_legacy_koolbar_rejected_delivery_is_not_automatically_retried(rule_db):
    routing_rules.save_target("Advertio Cargo", "@advertio_cargo")
    conn = rule_db()
    conn.execute("UPDATE transferlist SET departure_date='2099-01-01' WHERE id=1")
    conn.commit()
    conn.close()
    routing_rules.record_delivery(1, 1, "rejected", error="missing route")

    assert routed_publisher.RoutedPublisher._koolbar_pairs(limit=10) == []


def test_rule_and_koolbar_pairs_carry_raw_source_text(rule_db):
    routing_rules.save_target("Advertio Cargo", "@advertio_cargo")
    conn = rule_db()
    conn.execute("UPDATE transferlist SET departure_date='2099-01-01' WHERE id=1")
    conn.commit()
    conn.close()

    koolbar = routed_publisher.RoutedPublisher._koolbar_pairs(limit=10)
    assert koolbar[0][0]["raw_text"] == "original user text"

    routing_rules.save_target("Auto", "@autochannel")
    routing_rules.save_rule("All transfers", "transferlist", "", "either", 2)
    managed = routing_rules.pending(limit=10)
    assert managed[0][0]["raw_text"] == "original user text"


def test_legacy_koolbar_invalid_old_record_does_not_starve_valid_new_record(rule_db):
    routing_rules.save_target("Advertio Cargo", "@advertio_cargo")
    conn = rule_db()
    conn.execute("UPDATE transferlist SET departure_date='2099-01-01', destination_city='' WHERE id=1")
    conn.execute(
        "INSERT INTO messages VALUES(2,'transferlist','processed','processed',12,'bob','test','','second original user text','second legacy source text')"
    )
    conn.execute(
        "INSERT INTO transferlist VALUES(2,2,'Berlin','Toronto','DE','CA',200,'2099-01-02')"
    )
    conn.commit()
    conn.close()

    pairs = routed_publisher.RoutedPublisher._koolbar_pairs(limit=1)
    assert len(pairs) == 1
    assert pairs[0][0]["message_row_id"] == 2


def test_legacy_koolbar_diagnostics_explains_eligibility_and_delivery_blockers(rule_db):
    routing_rules.save_target("Advertio Cargo", "@advertio_cargo")
    conn = rule_db()
    conn.execute("UPDATE transferlist SET departure_date='2099-01-01' WHERE id=1")
    conn.commit()
    conn.close()

    diagnostics = routed_publisher.RoutedPublisher.koolbar_diagnostics(limit=10)
    assert diagnostics["eligible_count"] == 1
    assert diagnostics["rows"][0]["eligible"] is True
    assert diagnostics["rows"][0]["blockers"] == []

    routing_rules.record_delivery(1, 1, "rejected", error="missing route")
    diagnostics = routed_publisher.RoutedPublisher.koolbar_diagnostics(limit=10)
    assert diagnostics["eligible_count"] == 0
    assert diagnostics["rows"][0]["eligible"] is False
    assert diagnostics["rows"][0]["delivery_status"] == "rejected"
    assert "delivery status is rejected" in diagnostics["rows"][0]["blockers"]


def test_backoffice_diagnostics_reports_koolbar_fair_share(rule_db, monkeypatch):
    routing_rules.save_target("Advertio Cargo", "@advertio_cargo")
    target = routing_rules.list_targets()[0]

    monkeypatch.setattr(
        backoffice_web.routing_rules,
        "pending",
        lambda limit: [(None, {"target_id": 999}) for _ in range(limit)],
    )
    monkeypatch.setattr(backoffice_web.routing_rules, "is_rate_limited", lambda: False)
    monkeypatch.setattr(
        backoffice_web.RoutedPublisher,
        "koolbar_diagnostics",
        lambda limit=50: {
            "configured": True,
            "target_id": target["id"],
            "target_enabled": True,
            "today": "2099-01-01",
            "eligible_count": 30,
            "rows": [],
        },
    )

    output = backoffice_web._publishing_diagnostics_html(target, [])
    assert "Fair queue scheduling prevents managed rules" in output
    assert "50/50" in output
    assert "Legacy fair-share capacity" in output
    assert ">25<" in output
    assert "Advertio Cargo backlog exceeds one cycle" in output


def test_publisher_fairly_merges_rule_and_koolbar_queues():
    rule_pairs = [
        ({"message_row_id": index}, {"target_id": 100 + index})
        for index in range(1, 51)
    ]
    koolbar_pairs = [
        ({"message_row_id": 1000 + index}, {"id": 1, "target_id": 1})
        for index in range(1, 51)
    ]

    merged = routed_publisher.RoutedPublisher._merge_pending_pairs(
        rule_pairs,
        koolbar_pairs,
        limit=50,
    )

    assert len(merged) == 50
    assert sum(1 for _record, rule in merged if rule.get("target_id") == 1) == 25
    assert merged[0][0]["message_row_id"] == 1
    assert merged[1][0]["message_row_id"] == 1001


def test_publisher_fair_merge_deduplicates_same_message_and_target():
    duplicate_rule = ({"message_row_id": 1}, {"target_id": 1})
    duplicate_koolbar = ({"message_row_id": 1}, {"id": 1, "target_id": 1})
    extra_koolbar = ({"message_row_id": 2}, {"id": 1, "target_id": 1})

    merged = routed_publisher.RoutedPublisher._merge_pending_pairs(
        [duplicate_rule],
        [duplicate_koolbar, extra_koolbar],
        limit=2,
    )

    assert [(record["message_row_id"], rule.get("target_id")) for record, rule in merged] == [
        (1, 1),
        (2, 1),
    ]


def test_new_backoffice_rule_defaults_to_automatic_publishing(rule_db):
    routing_rules.save_target("Auto", "@autochannel")
    target = routing_rules.list_targets()[0]

    form = backoffice_web._rule_form(None, target, "csrf")
    assert '<option value="auto" selected>Automatic</option>' in form
    assert '<option value="manual" selected>' not in form


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


@pytest.mark.asyncio
async def test_channel_history_and_category_filter(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel", description="Transport ads")
    routing_rules.save_rule("Origin", "transferlist", "", "either", 1,
                            filter_field="origin_country", filter_value="TR")
    assert len(routing_rules.pending()) == 1
    routing_rules.record_delivery(1, 1, "rejected", error="Telegram sendMessage HTTP 403: forbidden")
    assert len(routing_rules.recent_deliveries(target_id=1)) == 1
    assert routing_rules.pending() == []
    routing_rules.update_target_connection(1, "disconnected", "Bot cannot post")
    assert routing_rules.pending() == []
    routing_rules.update_target_connection(1, "connected", "Bot can post")
    assert len(routing_rules.pending()) == 1
    response = await backoffice_web.index({"session": {"csrf": "test"}, "csp_nonce": "nonce"})
    assert "Transport ads" in response.text
    assert "Delivery history" in response.text
    assert "origin_country" in response.text


@pytest.mark.asyncio
async def test_rules_are_nested_under_channels_and_values_come_from_database(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_target("Other", "@otherchannel")
    routing_rules.save_rule("Turkey origin", "transferlist", "", "either", 1,
                            filter_field="origin_country", filter_value="TR")
    response = await backoffice_web.index({"session": {"csrf": "test"}, "csp_nonce": "nonce"})
    first = response.text.split('id="channel-1"', 1)[1].split('id="channel-2"', 1)[0]
    second = response.text.split('id="channel-2"', 1)[1]
    assert "Turkey origin" in first
    assert "Turkey origin" not in second
    assert 'name="target_id" value="1"' in first
    assert 'name="target_id" value="2"' in second
    assert "<h2>Rules</h2>" not in response.text

    class Request:
        query = {"category": "transferlist", "field": "origin_country"}

    response = await backoffice_web.filter_values(Request())
    assert response.text == '["TR"]'
    Request.query = {"category": "transferlist", "field": "invalid"}
    with pytest.raises(web.HTTPBadRequest):
        await backoffice_web.filter_values(Request())


@pytest.mark.asyncio
async def test_telegram_403_marks_destination_disconnected(rule_db, monkeypatch):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("All", "transferlist", "", "either", 1)

    class Response:
        status = 403
        ok = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs):
            return {"ok": False, "description": "Forbidden: bot is not a member of the channel chat"}

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr(routed_publisher.aiohttp, "ClientSession", Session)
    publisher = routed_publisher.RoutedPublisher(token="fake-token")
    assert (await publisher.publish_pending())["rejected"] == 1
    assert routing_rules.list_targets()[0]["connection_status"] == "disconnected"
    assert routing_rules.recent_deliveries(target_id=1)[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_manual_rule_previews_ad_and_does_not_auto_publish(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("Choose one", "transferlist", "", "either", 1,
                            filter_field="origin_country", filter_value="TR",
                            delivery_mode="manual")
    assert routing_rules.pending() == []
    rule, counts, records = routing_rules.rule_matches(1)
    assert rule["delivery_mode"] == "manual"
    assert counts == {"total": 1, "sent": 0}
    assert records[0]["message_row_id"] == 1
    assert routing_rules.selected_pair(1, 1)[0]["message_row_id"] == 1
    assert routing_rules.claim_delivery(1, 1)
    assert not routing_rules.claim_delivery(1, 1)
    routing_rules.record_delivery(1, 1, "sent", 900)
    with pytest.raises(ValueError):
        routing_rules.selected_pair(1, 1)
    response = await backoffice_web.index({"session": {"csrf": "test"}, "csp_nonce": "nonce"})
    assert "1 matching ads" in response.text
    assert "Delete rule" in response.text
    routing_rules.delete_rule(1, 1)
    assert routing_rules.list_rules() == []


def test_matching_ads_include_category_rows_before_ai_status_processed(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("All Turkey", "transferlist", "", "either", 1,
                            filter_field="origin_country", filter_value="TR",
                            delivery_mode="manual")
    conn = rule_db()
    conn.execute("INSERT INTO messages (id,ai_category,ai_status,processing_status,message_id,sender_username,channel_username,message_link) VALUES(2,NULL,'pending','pending',12,'bob','test','')")
    conn.execute("""INSERT INTO transferlist
        (id,processed_message_id,origin_city,destination_city,origin_country,destination_country,price)
        VALUES(2,2,'Ankara','Tehran','TR','IR',175)""")
    conn.commit()
    conn.close()

    _, counts, records = routing_rules.rule_matches(1)
    assert counts == {"total": 2, "sent": 0}
    assert [record["message_row_id"] for record in records] == [2, 1]
    assert all(record["ai_category"] == "transferlist" for record in records)


@pytest.mark.asyncio
async def test_sent_ad_can_be_manually_resent_without_overwriting_original(rule_db, monkeypatch):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("Auto rule", "transferlist", "", "either", 1)
    routing_rules.record_delivery(1, 1, "sent", 900)

    page = await backoffice_web.index({"session": {"csrf": "test"}, "csp_nonce": "nonce"})
    assert "Send again" in page.text
    assert "This ad was already delivered to this channel" in page.text

    monkeypatch.setattr(routed_publisher.config, "TELEGRAM_BOT_TOKEN", "fake-token")
    posted = []

    class Response:
        ok = True
        status = 200

        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs):
            return {"ok": True, "result": {"message_id": 901}}

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            posted.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(routed_publisher.aiohttp, "ClientSession", Session)

    class Request(dict):
        async def post(self):
            return {"rule_id": "1", "message_id": "1"}

    request = Request({"session": {"admin_id": 1485409432}})
    with pytest.raises(web.HTTPSeeOther):
        await backoffice_web.resend_selected(request)

    conn = rule_db()
    original = conn.execute("""SELECT status,telegram_message_id
        FROM publishing_deliveries WHERE message_id=1 AND target_id=1""").fetchone()
    resend = conn.execute("""SELECT requested_by,status,telegram_message_id
        FROM publishing_resends WHERE message_id=1 AND target_id=1""").fetchone()
    conn.close()
    assert tuple(original) == ("sent", 900)
    assert tuple(resend) == (1485409432, "sent", 901)
    assert posted[0]["chat_id"] == "@turkeychannel"
    history = routing_rules.recent_deliveries(target_id=1)
    assert any(item["delivery_kind"] == "resend" and item["telegram_message_id"] == 901
               for item in history)


@pytest.mark.asyncio
async def test_telegram_429_pauses_bot_without_repeated_sends(rule_db, monkeypatch):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("All", "transferlist", "", "either", 1)
    calls = []

    class Response:
        status = 429
        ok = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs):
            return {"ok": False, "description": "Too Many Requests: retry after 32",
                    "parameters": {"retry_after": 32}}

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, *args, **kwargs):
            calls.append(kwargs)
            return Response()

    monkeypatch.setattr(routed_publisher.aiohttp, "ClientSession", Session)
    publisher = routed_publisher.RoutedPublisher(token="fake-token")
    assert (await publisher.publish_pending())["rate_limited"] is True
    assert routing_rules.recent_deliveries(target_id=1)[0]["status"] == "retry"
    assert routing_rules.pending() == []
    assert (await publisher.publish_pending())["rate_limited"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_interrupted_send_requires_review_before_manual_retry(rule_db):
    routing_rules.save_target("Turkey", "@turkeychannel")
    routing_rules.save_rule("Select", "transferlist", "", "either", 1, delivery_mode="manual")
    assert routing_rules.claim_delivery(1, 1)
    conn = rule_db()
    conn.execute("UPDATE publishing_deliveries SET updated_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    _, _, records = routing_rules.rule_matches(1)
    assert records[0]["delivery_status"] == "uncertain"
    with pytest.raises(ValueError):
        routing_rules.selected_pair(1, 1)
    response = await backoffice_web.index({"session": {"csrf": "test"}, "csp_nonce": "nonce"})
    assert "Full ad preview" in response.text
    assert "Review and retry" in response.text
    routing_rules.retry_delivery(1, 1)
    assert routing_rules.selected_pair(1, 1)[0]["message_row_id"] == 1


def test_zero_category_value_matches_preview_and_auto_delivery(rule_db):
    conn = rule_db()
    conn.execute("ALTER TABLE joblist ADD COLUMN remote INTEGER")
    conn.execute("INSERT INTO messages (id,ai_category,ai_status,processing_status,message_id,sender_username,channel_username,message_link) VALUES(2,'joblist','processed','processed',12,'bob','sample_source','')")
    conn.execute("INSERT INTO joblist(processed_message_id,remote) VALUES(2,0)")
    conn.commit()
    conn.close()
    routing_rules.save_target("Jobs", "@jobchannel")
    routing_rules.save_rule("On site", "joblist", "", "either", 1,
                            filter_field="remote", filter_value="0")
    assert routing_rules.rule_matches(1)[1]["total"] == 1
    assert [record["message_row_id"] for record, _ in routing_rules.pending()] == [2]


def test_generic_conditions_use_live_database_columns(rule_db):
    conn = rule_db()
    conn.execute("ALTER TABLE joblist ADD COLUMN department TEXT")
    conn.execute("ALTER TABLE joblist ADD COLUMN remote INTEGER")
    conn.execute("INSERT INTO messages (id,ai_category,ai_status,processing_status,message_id,sender_username,channel_username,message_link) VALUES(2,'joblist','processed','processed',12,'bob','jobs_source','')")
    conn.execute("INSERT INTO joblist(processed_message_id,department,remote) VALUES(2,'Engineering',0)")
    conn.commit()
    conn.close()

    routing_rules.save_target("Jobs", "@jobchannel")
    routing_rules.save_rule(
        "Engineering on site", "joblist", "", "either", 1, delivery_mode="manual",
        conditions=[
            {"field": "department", "operator": "eq", "value": "Engineering"},
            {"join": "and", "field": "remote", "operator": "eq", "value": "0"},
        ],
    )

    assert "department" in routing_rules.category_fields("joblist")
    rule, counts, records = routing_rules.rule_matches(1)
    assert counts["total"] == 1
    assert records[0]["message_row_id"] == 2
    assert [item["field"] for item in routing_rules.effective_conditions(rule)] == ["department", "remote"]

    conn = rule_db()
    conn.execute("UPDATE joblist SET department='Sales' WHERE processed_message_id=2")
    conn.commit()
    conn.close()
    assert routing_rules.rule_matches(1)[1]["total"] == 0


def test_generic_or_conditions_match_left_to_right(rule_db):
    routing_rules.save_target("Routes", "@routechannel")
    routing_rules.save_rule(
        "Either side Turkey", "transferlist", "", "either", 1, delivery_mode="manual",
        conditions=[
            {"field": "origin_country", "operator": "eq", "value": "IR"},
            {"join": "or", "field": "destination_country", "operator": "eq", "value": "IR"},
            {"join": "and", "field": "price", "operator": "lte", "value": "200"},
        ],
    )
    assert routing_rules.rule_matches(1)[1]["total"] == 1
    conn = rule_db()
    conn.execute("UPDATE transferlist SET price=250")
    conn.commit()
    conn.close()
    assert routing_rules.rule_matches(1)[1]["total"] == 0


@pytest.mark.asyncio
async def test_backoffice_uses_generic_rule_builder_copy(rule_db):
    routing_rules.save_target("General", "@generalchannel")
    response = await backoffice_web.index({"session": {"csrf": "test"}, "csp_nonce": "nonce"})
    assert "+ Add channel / group" in response.text
    assert "+ Add condition" in response.text
    assert "Existing country, city and price filters" not in response.text
    assert "+ Add destination" not in response.text


def test_new_structured_topic_table_is_discovered_without_rule_code_change(rule_db):
    conn = rule_db()
    conn.execute("""CREATE TABLE eventlist (
        id INTEGER PRIMARY KEY,
        processed_message_id INTEGER UNIQUE,
        event_type TEXT,
        city TEXT
    )""")
    conn.execute("INSERT INTO messages (id,ai_category,ai_status,processing_status,message_id,sender_username,channel_username,message_link) VALUES(3,'eventlist','processed','processed',13,'carol','events_source','')")
    conn.execute("INSERT INTO eventlist(processed_message_id,event_type,city) VALUES(3,'conference','Toronto')")
    conn.commit()
    conn.close()

    assert "eventlist" in routing_rules.categories()
    assert routing_rules.category_fields("eventlist") == ("event_type", "city")

    routing_rules.save_target("Events", "@eventchannel")
    routing_rules.save_rule(
        "Toronto conferences", "eventlist", "", "either", 1, delivery_mode="manual",
        conditions=[
            {"field": "event_type", "operator": "eq", "value": "conference"},
            {"join": "and", "field": "city", "operator": "eq", "value": "Toronto"},
        ],
    )
    assert routing_rules.rule_matches(1)[1]["total"] == 1
