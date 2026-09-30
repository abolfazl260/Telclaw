"""Backoffice message-text editing guards; isolated SQLite and HTTP clients only."""

import json
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

import backoffice_data
import backoffice_web
import config
from storage import database


@pytest.fixture
def text_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "text-editor.sqlite3"))
    database.initialize_db()
    assert database.insert_message(
        "fixture", 99, "Legacy   content", "2026-09-29",
        raw_text="Original  Telegram\ncontent  📦",
        cleaned_text="Cleaned content", processing_status="processed",
    )
    with database.get_connection() as conn:
        row_id = conn.execute(
            "SELECT id FROM messages WHERE channel_username='fixture' AND message_id=99"
        ).fetchone()["id"]
    return row_id


def message_row(row_id):
    with database.get_connection() as conn:
        return dict(conn.execute(
            "SELECT raw_text, text, cleaned_text FROM messages WHERE id=?", (row_id,)
        ).fetchone())


def audit_rows(row_id):
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT admin_id,table_name,column_name,old_value,new_value "
            "FROM backoffice_data_edits WHERE row_id=? ORDER BY id", (row_id,)
        )]


def test_raw_source_requires_explicit_confirmation_and_preserves_other_columns(text_db):
    original = message_row(text_db)
    with pytest.raises(ValueError, match="Confirm editing"):
        backoffice_data.update_cell(
            "messages", text_db, "raw_text", "New original", original["raw_text"], 1234
        )
    assert message_row(text_db) == original

    assert backoffice_data.update_cell(
        "messages", text_db, "raw_text", "New original",
        original["raw_text"], 1234, confirm_raw_text=True
    ) == "New original"
    assert message_row(text_db) == {**original, "raw_text": "New original"}
    edits = audit_rows(text_db)
    assert len(edits) == 1
    assert edits[0]["column_name"] == "raw_text"
    assert json.loads(edits[0]["old_value"]) == original["raw_text"]
    assert json.loads(edits[0]["new_value"]) == "New original"

    # No stale raw edits even with confirmation.
    with pytest.raises(backoffice_data.ConflictError):
        backoffice_data.update_cell(
            "messages", text_db, "raw_text", "Stale overwrite",
            original["raw_text"], 1234, confirm_raw_text=True
        )
    assert len(audit_rows(text_db)) == 1


def test_cleaned_text_edit_is_independent_by_default(text_db):
    original = message_row(text_db)
    assert backoffice_data.update_cell(
        "messages", text_db, "cleaned_text", "New AI text",
        original["cleaned_text"], 1234,
    ) == "New AI text"
    assert message_row(text_db) == {**original, "cleaned_text": "New AI text"}
    assert [r["column_name"] for r in audit_rows(text_db)] == ["cleaned_text"]

    assert backoffice_data.update_cell(
        "messages", text_db, "text", "New legacy copy", original["text"], 1234
    ) == "New legacy copy"
    assert message_row(text_db)["cleaned_text"] == "New AI text"
    assert [r["column_name"] for r in audit_rows(text_db)] == ["cleaned_text", "text"]


def test_sync_is_opt_in_cas_atomic_and_audits_both_values(text_db):
    original = message_row(text_db)
    info = backoffice_data.cell("messages", text_db, "cleaned_text")
    assert info["value"] == "Cleaned content"
    assert info["mirror_value"] == "Legacy   content"
    assert info["divergent"] is True

    assert backoffice_data.update_cell(
        "messages", text_db, "cleaned_text", "  Edited AI text  ",
        info["value"], 1234, sync_text=True, mirror_expected=info["mirror_value"],
    ) == "  Edited AI text  "
    row = message_row(text_db)
    assert row["raw_text"] == original["raw_text"]
    assert row["text"] == row["cleaned_text"] == "  Edited AI text  "
    edits = audit_rows(text_db)
    assert [r["column_name"] for r in edits] == ["cleaned_text", "text"]
    assert all(r["admin_id"] == 1234 and r["table_name"] == "messages" for r in edits)
    assert json.loads(edits[0]["old_value"]) == original["cleaned_text"]
    assert json.loads(edits[1]["old_value"]) == original["text"]
    assert all(json.loads(r["new_value"]) == "  Edited AI text  " for r in edits)
    assert backoffice_data.cell("messages", text_db, "cleaned_text")["divergent"] is False

    with pytest.raises(backoffice_data.ConflictError):
        backoffice_data.update_cell(
            "messages", text_db, "cleaned_text", "Stale AI text",
            info["value"], 1234, sync_text=True, mirror_expected=info["mirror_value"],
        )
    assert message_row(text_db) == row
    assert len(audit_rows(text_db)) == 2


def test_sync_aborts_if_legacy_text_changed_after_editor_opened(text_db):
    snapshot = backoffice_data.cell("messages", text_db, "cleaned_text")
    assert backoffice_data.update_cell(
        "messages", text_db, "text", "Concurrent legacy edit",
        snapshot["mirror_value"], 999,
    ) == "Concurrent legacy edit"
    before = message_row(text_db)

    with pytest.raises(backoffice_data.ConflictError):
        backoffice_data.update_cell(
            "messages", text_db, "cleaned_text", "Unsafe overwrite",
            snapshot["value"], 1234, sync_text=True,
            mirror_expected=snapshot["mirror_value"],
        )
    assert message_row(text_db) == before
    assert [r["column_name"] for r in audit_rows(text_db)] == ["text"]


def test_null_and_empty_mirror_are_distinct_and_sync_does_not_touch_raw(text_db):
    original = message_row(text_db)
    with database.get_connection() as conn:
        conn.execute("UPDATE messages SET text=NULL WHERE id=?", (text_db,))
        conn.commit()
    info = backoffice_data.cell("messages", text_db, "cleaned_text")
    assert info["mirror_value"] is None
    assert info["divergent"] is True
    backoffice_data.update_cell(
        "messages", text_db, "cleaned_text", "",
        info["value"], 1234, sync_text=True, mirror_expected=None
    )
    assert message_row(text_db) == {
        "raw_text": original["raw_text"], "cleaned_text": "", "text": "",
    }
    edits = audit_rows(text_db)
    assert json.loads(edits[1]["old_value"]) is None
    assert json.loads(edits[1]["new_value"]) == ""
    assert backoffice_data.page("messages", 1, {"text": {"op": "empty"}})["total"] == 1
    assert backoffice_data.page("messages", 1, {"text": {"op": "null"}})["total"] == 0


def test_mirror_sync_validation_does_not_change_any_cell(text_db):
    before = message_row(text_db)
    with pytest.raises(ValueError, match="only when editing cleaned_text"):
        backoffice_data.update_cell(
            "messages", text_db, "text", "invalid sync",
            before["text"], 1234, sync_text=True, mirror_expected=before["text"],
        )
    with pytest.raises(ValueError, match="Original legacy text value"):
        backoffice_data.update_cell(
            "messages", text_db, "cleaned_text", "invalid sync",
            before["cleaned_text"], 1234, sync_text=True,
        )
    assert message_row(text_db) == before
    assert audit_rows(text_db) == []


@pytest.mark.asyncio
async def test_http_editor_labels_guards_csrf_and_atomic_sync(text_db):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        assert (await client.get("/data")).status == 401
        session_token, csrf = "text-guard-session", "text-guard-csrf"
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                (
                    backoffice_web._digest(session_token), 1485409432, csrf,
                    (backoffice_web._now() + timedelta(hours=1)).isoformat(),
                ),
            )
            conn.commit()
        headers = {"Cookie": f"telclaw_admin={session_token}"}
        response = await client.get("/data?table=messages", headers=headers)
        assert response.status == 200
        page = await response.text()
        assert 'name="op_text"' in page and 'name="f_raw_text"' in page
        assert "Original Telegram payload" in page
        assert "Processed text preferred by AI" in page
        assert "Legacy compatibility copy" in page
        assert "Differs from cleaned_text" in page
        assert "confirm_raw_text" in page and "sync_text" in page

        async def get_cell(column):
            resp = await client.get(
                f"/data/cell?table=messages&id={text_db}&column={column}",
                headers=headers,
            )
            assert resp.status == 200
            return await resp.json()

        source_info = await get_cell("raw_text")
        body = dict(csrf=csrf, table="messages", id=str(text_db), column="raw_text",
                    value="New original", expected=json.dumps(source_info["value"]),
                    make_null="0")
        rejected = await client.post("/data/cell", data=body, headers=headers)
        assert rejected.status == 400
        assert message_row(text_db)["raw_text"] == source_info["value"]
        assert (await client.post("/data/cell", data={**body, "csrf": "bad",
                 "confirm_raw_text": "1"}, headers=headers)).status == 403
        accepted = await client.post(
            "/data/cell", data={**body, "confirm_raw_text": "1"}, headers=headers
        )
        assert accepted.status == 200

        cleaned_info = await get_cell("cleaned_text")
        sync = dict(csrf=csrf, table="messages", id=str(text_db), column="cleaned_text",
                    value="New AI input", expected=json.dumps(cleaned_info["value"]),
                    make_null="0", sync_text="1",
                    mirror_expected=json.dumps(cleaned_info["mirror_value"]))
        accepted = await client.post("/data/cell", data=sync, headers=headers)
        assert accepted.status == 200
        assert message_row(text_db) == {
            "raw_text": "New original", "cleaned_text": "New AI input", "text": "New AI input",
        }
        assert (await client.post("/data/cell", data=sync, headers=headers)).status == 409
        assert (await client.post("/data/cell", data={**sync, "csrf": "invalid"},
                                  headers=headers)).status == 403
        assert [r["column_name"] for r in audit_rows(text_db)] == [
            "raw_text", "cleaned_text", "text",
        ]
    finally:
        await client.close()
