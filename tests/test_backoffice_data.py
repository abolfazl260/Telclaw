"""End-to-end checks for the paginated, cell-based back office editor."""
import json
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

import backoffice_data
import backoffice_web
import config
from storage import database


@pytest.fixture
def data_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "telclaw.sqlite3"))
    database.initialize_db()
    conn = database.get_connection()
    try:
        for i in range(1, 29):
            conn.execute("""INSERT INTO messages(channel_username,message_id,date,text)
                VALUES(?,?,?,?)""", ("source", i, "2026-09-28", f"Ad {i}"))
        conn.execute("""INSERT INTO transferlist(processed_message_id,origin_city,price)
            VALUES(1,'Old',10)""")
        conn.commit()
    finally:
        conn.close()


def test_page_and_checked_cell_updates(data_db):
    first = backoffice_data.page("messages", 1)
    assert first["total"] == 28 and len(first["rows"]) == 25
    assert len(backoffice_data.page("messages", 2)["rows"]) == 3
    assert backoffice_data.page("messages", 999)["page"] == 2
    assert "text" in first["editable"] and "ai_status" not in first["editable"]
    assert backoffice_data.update_cell("transferlist", 1, "origin_city", "New", "Old", 1485409432) == "New"
    with pytest.raises(backoffice_data.ConflictError):
        backoffice_data.update_cell("transferlist", 1, "origin_city", "Stale", "Old", 1485409432)
    with pytest.raises(ValueError):
        backoffice_data.update_cell("transferlist", 1, "processed_message_id", "2", 1, 1485409432)
    with pytest.raises(ValueError):
        backoffice_data.page("backoffice_sessions")
    assert backoffice_data.update_cell("transferlist", 1, "price", "0", 10, 1485409432) == 0
    assert backoffice_data.update_cell("transferlist", 1, "origin_city", "", "New", 1485409432,
                                      make_null=True) is None
    assert backoffice_data.cell("transferlist", 1, "origin_city")["value"] is None
    conn = database.get_connection()
    try:
        edits = conn.execute("SELECT admin_id,old_value,new_value FROM backoffice_data_edits").fetchall()
        assert len(edits) == 3 and all(row["admin_id"] == 1485409432 for row in edits)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_editor_requires_login_and_csrf_and_reports_conflicts(data_db):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        response = await client.get("/data")
        assert response.status == 401
        session_token, csrf = "local-session", "csrf-test"
        conn = database.get_connection()
        try:
            conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                         (backoffice_web._digest(session_token), 1485409432, csrf,
                          (backoffice_web._now() + timedelta(hours=1)).isoformat()))
            conn.commit()
        finally:
            conn.close()
        headers = {"Cookie": f"telclaw_admin={session_token}"}
        response = await client.get("/data?table=messages&page=2", headers=headers)
        assert response.status == 200
        page = await response.text()
        assert "Page 2 of 2" in page and "edit-cell" in page and "Database" in page
        response = await client.get("/data/cell?table=transferlist&id=1&column=origin_city",
                                    headers=headers)
        assert (await response.json())["value"] == "Old"
        body = {"csrf": csrf, "table": "transferlist", "id": "1", "column": "origin_city",
                "value": "Tehran", "expected": json.dumps("Old"), "make_null": "0"}
        response = await client.post("/data/cell", data=body, headers=headers)
        assert response.status == 200 and (await response.json())["value"] == "Tehran"
        response = await client.post("/data/cell", data=body, headers=headers)
        assert response.status == 409
        response = await client.post("/data/cell", data={**body, "csrf": "wrong"}, headers=headers)
        assert response.status == 403
        response = await client.post("/data/cell", data={**body, "column": "ai_status"}, headers=headers)
        assert response.status == 400
    finally:
        await client.close()
