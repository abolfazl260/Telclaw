"""Report formatting for /transferlive when a route has partial location data."""

from monitoring import transfer_live


def _row(message_id, origin="IR", destination="DE", city=None):
    return {
        "message_row_id": message_id,
        "sender_id": message_id,
        "sender_username": f"user{message_id}",
        "origin_country": origin,
        "origin_city": "Tehran",
        "destination_country": destination,
        "destination_city": city,
        "departure_date": "2099-10-18",
    }


def test_destination_with_country_but_no_city_displays_flag_and_dash():
    assert transfer_live._destination_label(_row(1)) == "\u2066🇩🇪 —\u2069"
    assert transfer_live._destination_label(_row(1, city="نامشخص")) == "\u2066🇩🇪 —\u2069"
    assert transfer_live._destination_label(_row(1, city="unknown")) == "\u2066🇩🇪 —\u2069"
    assert transfer_live._destination_label(_row(1, city="Düsseldorf")) == "\u2066🇩🇪 Düsseldorf\u2069"


def test_unknown_destination_country_does_not_render_fallback_city_or_flag():
    assert transfer_live._destination_label(_row(1, destination=None, city="Berlin")) == ""
    assert transfer_live._destination_label(_row(1, destination="??", city="Berlin")) == ""
    assert transfer_live._destination_label(_row(1, destination="ZZ", city="Berlin")) == ""


def test_transferlive_excludes_unknown_countries_and_updates_counts(monkeypatch):
    monkeypatch.setattr(transfer_live, "_fetch_active_rows", lambda: [
        _row(1, city=None),
        _row(2, city="Düsseldorf"),
        _row(3, destination=None, city="Berlin"),
        _row(4, origin=None, city="Berlin"),
        _row(5, destination="نامشخص", city="Berlin"),
        _row(6, destination="XX", city="Berlin"),
    ])

    monkeypatch.setattr(transfer_live, "_fetch_published_transfer_stats", lambda: {"last_30_days": 246})
    messages = transfer_live._build_messages()
    assert len(messages) == 1
    html = messages[0]["html"]
    assert "<b>۲</b> آگهی در <b>۱</b> مبدا" in html
    assert "<caption>۲ آگهی فعال</caption>" in html
    assert "<details><summary>📍 از مبدا 🇮🇷 Iran — <b>۲</b> آگهی فعال</summary>" in html
    assert "</table></details>" in html
    assert "<b>۲۴۶</b> آگهی ارسال بار در <b>۳۰</b> روز گذشته منتشر شده است" in html
    assert "🇩🇪 —" in html
    assert "🇩🇪 Düsseldorf" in html
    assert "نامشخص" not in html
    assert "user1" in html and "user2" in html
    for missing in ("user3", "user4", "user5", "user6"):
        assert missing not in html


def test_transferlive_empty_state_when_no_known_country(monkeypatch):
    monkeypatch.setattr(transfer_live, "_fetch_active_rows", lambda: [
        _row(1, destination=None),
        _row(2, origin=None),
    ])
    monkeypatch.setattr(transfer_live, "_fetch_published_transfer_stats", lambda: {"last_30_days": 0})
    html = transfer_live._build_messages()[0]["html"]
    assert "در حال حاضر آگهی فعال حمل‌ونقل وجود ندارد" in html
    assert "<table" not in html
    assert "نامشخص" not in html


def test_country_summary_skips_invalid_codes_but_keeps_valid_unlisted_codes():
    summary = transfer_live._country_summary_rows([
        _row(1, origin="IR", destination="DE"),
        _row(2, origin="IR", destination=""),
        _row(3, origin="", destination="DE"),
        _row(4, origin="XX", destination="DE"),
        _row(5, origin="IR", destination="??"),
        _row(6, origin="IR", destination="AU"),
    ])
    assert summary == [(("IR", "AU"), 1), (("IR", "DE"), 1)]


def test_live_header_uses_dynamic_departures_and_linked_contacts(monkeypatch):
    from datetime import datetime, timedelta

    today = datetime.now(transfer_live.TEHRAN_TZ).date()
    entries = [
        {**_row(1), "departure_date": today.isoformat()},
        {**_row(2), "departure_date": (today + timedelta(days=1)).isoformat()},
        {**_row(3), "departure_date": (today + timedelta(days=2)).isoformat()},
        {**_row(4), "departure_date": (today + timedelta(days=20)).isoformat()},
    ]
    monkeypatch.setattr(transfer_live, "_fetch_active_rows", lambda: entries)
    monkeypatch.setattr(transfer_live, "_fetch_published_transfer_stats", lambda: {"last_30_days": 246})
    html = transfer_live._build_messages()[0]["html"]
    assert "<h1>🟢 آگهی های فعال Advertio</h1>" in html
    assert "<b>۴</b> آگهی در <b>۱</b> مبدا" in html
    assert "<b>۲</b> آگهی تا فردا منقضی و پرواز خواهند کرد" in html
    assert '<a href="https://t.me/advertio_bot">@advertio_bot</a>' in html
    assert '<a href="https://t.me/advertio_support">@advertio_support</a>' in html
    assert "<tg-button" not in html


def test_live_accordion_chunks_are_complete_and_within_limits(monkeypatch):
    from datetime import datetime, timedelta

    date = (datetime.now(transfer_live.TEHRAN_TZ).date() + timedelta(days=30)).isoformat()
    entries = [
        {**_row(i, city="Moscow & Dubai"), "departure_date": date,
         "sender_username": f"long_username_{i:04d}" + "x" * 32}
        for i in range(1, 450)
    ]
    monkeypatch.setattr(transfer_live, "_fetch_active_rows", lambda: entries)
    monkeypatch.setattr(transfer_live, "_fetch_published_transfer_stats", lambda: {"last_30_days": 0})
    chunks = transfer_live._build_messages()
    assert len(chunks) > 1
    assert all(len(chunk["html"].encode("utf-8")) <= transfer_live.MAX_RICH_MESSAGE_LENGTH for chunk in chunks)
    assert all(chunk["html"].count("<details>") == chunk["html"].count("</details>") for chunk in chunks)
    assert all(chunk["html"].count("<table ") == chunk["html"].count("</table>") for chunk in chunks)
    combined = "".join(chunk["html"] for chunk in chunks)
    assert combined.count("<td ") == 449 * 5
    assert "Moscow &amp; Dubai" in combined
    assert "بخش ۱" in combined and "بخش ۲" in combined
