"""Filtered database exports must never fall back to entire-table downloads."""
import csv
import io
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

import backoffice_data
import backoffice_web
import config
from storage import database


@pytest.fixture
def filtered_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "filtered_export.sqlite"))
    database.initialize_db()
    conn = database.get_connection()
    try:
        for i in range(1, 35):
            conn.execute(
                "INSERT INTO messages(channel_username,message_id,date,text) VALUES(?,?,?,?)",
                ("cargo_channel", i, "2026-10-22", f"Message {i}"),
            )
            origin = "Tehran" if i <= 29 else "Rome"
            description = (
                '=HYPERLINK("https://example.com","click")' if i == 1
                else "ارسال مدارک به Москва" if i == 2
                else "Safe cargo data"
            )
            conn.execute(
                "INSERT INTO transferlist(processed_message_id,origin_city,description,price) "
                "VALUES(?,?,?,?)",
                (i, origin, description, 12 if i <= 29 else 1),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_filtered_csv_exports_all_matching_rows_not_page_or_whole_database(filtered_db):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        assert (await client.get("/data/export?table=transferlist&op_origin_city=eq&f_origin_city=Tehran")).status == 401
        session_token = "valid-test-session"
        conn = database.get_connection()
        try:
            conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                         (backoffice_web._digest(session_token), 1485409432, "secret-csrf",
                          (backoffice_web._now() + timedelta(hours=1)).isoformat()))
            conn.commit()
        finally:
            conn.close()
        headers = {"Cookie": f"telclaw_admin={session_token}"}
        query = "?table=transferlist&page=2&op_origin_city=eq&f_origin_city=Tehran"
        page = await client.get("/data" + query, headers=headers)
        assert page.status == 200
        markup = await page.text()
        assert "29 rows · Page 2 of 2" in markup
        assert "Download filtered CSV (29 rows)" in markup
        assert "/data/export?table=transferlist" in markup

        response = await client.get("/data/export" + query, headers=headers)
        assert response.status == 200
        assert "attachment" in response.headers["Content-Disposition"]
        assert "telclaw-transferlist-filtered" in response.headers["Content-Disposition"]
        assert "text/csv" in response.headers["Content-Type"]
        raw = await response.read()
        assert raw.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows) == 29  # not 25, not all 34
        assert {row["origin_city"] for row in rows} == {"Tehran"}
        assert all(float(row["price"]) == 12 for row in rows)
        assert any("ارسال مدارک به Москва" == row["description"] for row in rows)
        assert next(row for row in rows if row["processed_message_id"] == "1")["description"].startswith("'=HYPERLINK")
        assert {row["processed_message_id"] for row in rows} == {str(i) for i in range(1, 30)}

        # Numeric range and NULL checks use the same filtering machinery.
        response = await client.get(
            "/data/export?table=transferlist&op_price=gte&f_price=12&op_origin_city=not_null",
            headers=headers,
        )
        assert response.status == 200
        assert len(list(csv.DictReader(io.StringIO((await response.read()).decode("utf-8-sig"))))) == 29
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_filtered_export_refuses_unfiltered_and_invalid_requests(filtered_db):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        token = "valid-test-session"
        conn = database.get_connection()
        try:
            conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                         (backoffice_web._digest(token), 1485409432, "test-csrf",
                          (backoffice_web._now() + timedelta(hours=1)).isoformat()))
            conn.commit()
        finally:
            conn.close()
        headers = {"Cookie": f"telclaw_admin={token}"}
        for query in (
            "?table=transferlist",
            "?table=transferlist&op_origin_city=eq",  # empty filter input
            "?table=transferlist&op_price=gte&f_price=",
            "?table=backoffice_sessions&op_admin_id=eq&f_admin_id=1485409432",
            "?table=transferlist&op_no_such_column=eq&f_no_such_column=1",
            "?table=transferlist&op_origin_city=invalid&f_origin_city=Tehran",
            "?table=transferlist&op_price=gte&f_price=not-a-number",
        ):
            response = await client.get("/data/export" + query, headers=headers)
            assert response.status == 400, query

        page = await client.get("/data?table=transferlist", headers=headers)
        markup = await page.text()
        assert "Apply a filter to enable CSV download" in markup
        assert "Download filtered CSV (" not in markup

        # No edit/create action, and no download without an active filter.
        assert backoffice_data.page("transferlist")["total"] == 34
    finally:
        await client.close()
