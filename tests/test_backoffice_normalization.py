from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

import backoffice_web
import config
from storage import database
from storage import data_normalizer


@pytest.fixture
def web_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "normalization-web.sqlite3"))
    database.initialize_db()
    data_normalizer.initialize()


def _session():
    token, csrf = "normalization-session", "normalization-csrf"
    conn = database.get_connection()
    try:
        conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                     (backoffice_web._digest(token), 1485409432, csrf,
                      (backoffice_web._now() + timedelta(hours=1)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    return token, csrf


@pytest.mark.asyncio
async def test_admin_can_add_alias_and_backfill_from_normalization_tab(web_db):
    message_id = None
    conn = database.get_connection()
    try:
        conn.execute("""INSERT INTO messages(channel_username,message_id,date,raw_text)
            VALUES('source',1,'2026-09-29','NYC raw source')""")
        message_id = conn.execute("SELECT id FROM messages WHERE message_id=1").fetchone()["id"]
        conn.execute("INSERT INTO housinglist(processed_message_id,city,country_code) VALUES(?,?,?)",
                     (message_id, "NYC", "US"))
        conn.commit()
    finally:
        conn.close()

    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        token, csrf = _session()
        headers = {"Cookie": f"telclaw_admin={token}"}

        response = await client.get("/normalization", headers=headers)
        assert response.status == 200
        page = await response.text()
        assert "Data Normalization" in page
        assert "Add normalization alias" in page
        assert "transferlist · origin_city" in page

        response = await client.post("/normalization/save", headers=headers, data={
            "csrf": csrf,
            "target": "housinglist.city",
            "alias": "NYC",
            "canonical": "New York",
            "country_iso2": "US",
            "notes": "User-managed alias",
            "enabled": "1",
        }, allow_redirects=False)
        assert response.status == 303

        aliases = [row for row in data_normalizer.list_aliases()
                   if row["category"] == "housinglist" and row["field_name"] == "city"
                   and row["alias"] == "NYC"]
        assert len(aliases) == 1
        assert data_normalizer.count_current_matches(aliases[0]["id"]) == 1

        response = await client.post("/normalization/apply", headers=headers, data={
            "csrf": csrf,
            "category": "housinglist",
        }, allow_redirects=False)
        assert response.status == 303

        conn = database.get_connection()
        try:
            row = conn.execute("SELECT city FROM housinglist WHERE processed_message_id=?",
                               (message_id,)).fetchone()
            raw = conn.execute("SELECT raw_text FROM messages WHERE id=?", (message_id,)).fetchone()
        finally:
            conn.close()
        assert row["city"] == "New York"
        assert raw["raw_text"] == "NYC raw source"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_normalization_mutations_require_csrf(web_db):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        token, _ = _session()
        headers = {"Cookie": f"telclaw_admin={token}"}
        response = await client.post("/normalization/save", headers=headers, data={
            "csrf": "wrong",
            "target": "transferlist.origin_city",
            "alias": "X",
            "canonical": "Y",
            "enabled": "1",
        })
        assert response.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cargo_city_rules_are_visible_and_can_be_applied_independently(web_db):
    conn = database.get_connection()
    try:
        conn.execute("""INSERT INTO messages(channel_username,message_id,date,raw_text)
            VALUES('cargo_source',42,'2026-10-22','source text must remain')""")
        message_id = conn.execute(
            "SELECT id FROM messages WHERE message_id=42"
        ).fetchone()["id"]
        conn.execute("""INSERT INTO transferlist(
            processed_message_id,origin_city,origin_country,destination_city,
            destination_country,description)
            VALUES(?,?,?,?,?,?)""",
            (message_id, "Teheran", "Iran", "تورنتو", "Canada", "cleaned text unchanged"))
        conn.commit()
    finally:
        conn.close()

    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        token, csrf = _session()
        headers = {"Cookie": f"telclaw_admin={token}"}
        response = await client.get("/normalization?view=cargo", headers=headers)
        assert response.status == 200
        page = await response.text()
        assert "Cargo city/country rules" in page
        assert 'action="/normalization/apply-cargo-locations"' in page
        assert "Cargo Location Normalization" in page
        assert "<details id=\"advanced-normalization\">" in page
        assert page.count('action="/normalization/apply-cargo-locations"') == 1
        assert 'action="/normalization/fill-transfer-provinces"' not in page
        assert 'action="/normalization/apply-transfer-cities"' not in page
        assert "Load first 20 rules" in page
        assert 'id="alias-' not in page  # do not render hundreds of forms initially
        assert page.index("Cargo Location Normalization") < page.index("Existing aliases")

        response = await client.get("/normalization?view=cargo&rules=1", headers=headers)
        assert response.status == 200
        page = await response.text()
        assert "Showing page 1" in page
        assert 'id="alias-' in page

        no_csrf = await client.post("/normalization/apply-cargo-locations",
                                    headers=headers, data={"csrf": "wrong"})
        assert no_csrf.status == 403
        response = await client.post("/normalization/apply-cargo-locations",
                                     headers=headers, data={"csrf": csrf},
                                     allow_redirects=False)
        assert response.status == 303
        assert "notice=cargo_applied" in response.headers["Location"]

        conn = database.get_connection()
        try:
            row = conn.execute("SELECT * FROM transferlist WHERE processed_message_id=?",
                               (message_id,)).fetchone()
            raw = conn.execute("SELECT raw_text FROM messages WHERE id=?",
                               (message_id,)).fetchone()[0]
        finally:
            conn.close()
        assert row["origin_city"] == "Tehran"
        assert row["origin_country"] == "IR"
        assert row["destination_city"] == "Toronto"
        assert row["destination_country"] == "CA"
        assert row["description"] == "cleaned text unchanged"
        assert raw == "source text must remain"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_normalization_defers_rule_cards_and_match_scans_until_requested(web_db, monkeypatch):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        token, _ = _session()
        headers = {"Cookie": f"telclaw_admin={token}"}
        real_count = data_normalizer.count_current_matches
        calls = []
        def count(rule_id):
            calls.append(rule_id)
            return real_count(rule_id)
        monkeypatch.setattr(data_normalizer, "count_current_matches", count)

        response = await client.get("/normalization", headers=headers)
        assert response.status == 200
        initial = await response.text()
        assert "Load first 20 rules" in initial
        assert "Check affected rows" not in initial
        assert 'id="alias-' not in initial
        assert len(calls) == 0
        assert initial.index("Re-normalize existing data") < initial.index("Existing aliases")

        response = await client.get("/normalization?rules=1", headers=headers)
        page1 = await response.text()
        assert response.status == 200
        assert page1.count('class="alias-card"') == 20
        assert "Load next 20" in page1
        assert "Check affected rows" in page1
        assert len(calls) == 0

        response = await client.get("/normalization?rules=1&page=2", headers=headers)
        page2 = await response.text()
        assert response.status == 200
        assert page2.count('class="alias-card"') == 20
        assert "Previous 20" in page2
        assert len(calls) == 0

        # A single Check affected rows action counts only that alias, and
        # doesn't recompute match counts for the other 19 displayed rules.
        total, rows = data_normalizer.paged_aliases(limit=20, offset=0)
        assert total > 20
        rule_id = rows[0]["id"]
        response = await client.get(
            f"/normalization?rules=1&count={rule_id}", headers=headers
        )
        assert response.status == 200
        assert "existing value(s) would change" in (await response.text())
        assert calls == [rule_id]

        # Cargo-only list applies filtering in SQLite before pagination.
        total_cargo, cargo_page = data_normalizer.paged_aliases(
            cargo_only=True, limit=20, offset=0
        )
        assert total_cargo > 20
        assert len(cargo_page) == 20
        assert all(
            rule["category"] == "transferlist"
            and rule["field_name"] in {
                "origin_city", "destination_city",
                "origin_country", "destination_country",
            }
            for rule in cargo_page
        )
        response = await client.get(
            "/normalization?view=cargo&rules=1", headers=headers
        )
        assert response.status == 200
        assert (await response.text()).count('class="alias-card"') == 20
        assert calls == [rule_id]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_normalization_rejects_invalid_rule_pages_without_db_scans(web_db):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        token, _ = _session()
        headers = {"Cookie": f"telclaw_admin={token}"}
        for params in ("?rules=1&page=0", "?rules=1&page=abc",
                       "?rules=1&page=99999", "?rules=1&count=-1"):
            response = await client.get("/normalization" + params, headers=headers)
            assert response.status == 400, params
    finally:
        await client.close()
