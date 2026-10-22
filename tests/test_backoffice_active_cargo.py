"""Back Office active cargo UI regressions; no external API calls."""
from datetime import date, timedelta
from types import SimpleNamespace
from multidict import MultiDict
import pytest
import config
import backoffice_cargo_page
from storage import database


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "active-cargo-ui.sqlite"))
    monkeypatch.setattr(config, "ADVERTIO_CARGO_BACKFILL_DAYS", 30)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_CARGO_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_KEY", "not-a-real-secret")
    database.initialize_db()
    database.insert_message(
        "safe_channel", 1200, "Source description",
        date.today().isoformat(), message_link="https://t.me/safe_channel/1200",
        raw_text="Source description",
    )
    database.update_message(
        1200, "safe_channel",
        processing_status="processed", ai_status="processed", ai_category="transferlist",
    )
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM messages WHERE channel_username='safe_channel' AND message_id=1200"
        ).fetchone()
        row_id = row[0]
    finally:
        conn.close()
    database.save_category_record(row_id, "transferlist", {
        "title": "Toronto document transport",
        "description": "Untrusted <script>alert(7)</script> details",
        "origin_country": "CA", "origin_city": "Toronto",
        "destination_country": "IR", "destination_city": "Tehran",
        "departure_date": (date.today() + timedelta(days=5)).isoformat(),
    })
    return row_id


class FakeOps:
    def __init__(self):
        self.selected = None
        self.batch = None
    def state(self, name):
        return {"status": "idle", "result": None, "error": None}
    def start_selected_advertio_cargo(self, ids, requested_by):
        self.selected = (list(ids), requested_by)
    def start_advertio_cargo(self, limit, requested_by):
        self.batch = (limit, requested_by)


class FakeRequest(dict):
    def __init__(self, *, query=None, form=None, operations=None):
        super().__init__({"session": {"csrf": "known-csrf", "admin_id": 7},
                          "csp_nonce": "nonce"})
        self.query = query or {}
        self.app = {"operations": operations or FakeOps()}
        self._form = MultiDict(form or [])
    async def post(self):
        return self._form


@pytest.mark.asyncio
async def test_active_cargo_page_renders_safe_active_report(isolated_db):
    request = FakeRequest()
    response = await backoffice_cargo_page.cargo_page(request)
    assert response.status == 200
    assert "Active Cargo · Advertio" in response.text
    assert "Active cargo overview" in response.text
    assert "Toronto document transport" in response.text
    assert 'name="record_id"' in response.text
    assert "Send selected active cargo to Advertio" in response.text
    assert "Send next 25 ready ads" in response.text
    assert "/operations#advertio-delivery" in response.text
    assert "<script>alert(7)</script>" not in response.text
    assert "&lt;script&gt;alert(7)&lt;/script&gt;" in response.text
    assert "not-a-real-secret" not in response.text


@pytest.mark.asyncio
async def test_active_cargo_page_applies_filters(isolated_db):
    response = await backoffice_cargo_page.cargo_page(
        FakeRequest(query={"status": "sent", "search": "nothing"})
    )
    assert "No cargo ads match" in response.text
    with pytest.raises(Exception) as raised:
        await backoffice_cargo_page.cargo_page(
            FakeRequest(query={"status": "malicious OR 1=1"})
        )
    assert getattr(raised.value, "status", None) == 400


@pytest.mark.asyncio
async def test_cargo_selected_and_batch_submission(isolated_db):
    ops = FakeOps()
    with pytest.raises(Exception) as selected_redirect:
        await backoffice_cargo_page.cargo_send(
            FakeRequest(operations=ops, form=[("record_id", str(isolated_db))])
        )
    assert getattr(selected_redirect.value, "status", None) == 303
    assert ops.selected == ([str(isolated_db)], 7)
    with pytest.raises(Exception) as batch_redirect:
        await backoffice_cargo_page.cargo_send(
            FakeRequest(operations=ops, form=[("mode", "next")])
        )
    assert getattr(batch_redirect.value, "status", None) == 303
    assert ops.batch == (25, 7)
