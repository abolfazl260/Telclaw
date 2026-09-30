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
