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
    assert "<b>۲ مسافر فعال</b> در <b>۱ مبدأ</b>" in html
    assert "<caption>" not in html
    assert "<details><summary>📍 از مبدا 🇮🇷 Iran — <b>۲</b></summary>" in html
    assert "</table></details>" in html
    assert "<b>۲۴۶ آگهی</b> منتشرشده در ۳۰ روز گذشته" in html
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
    assert "<h1>🟢 آگهی‌های فعال | Advertio</h1>" in html
    assert "<b>۴ مسافر فعال</b> در <b>۱ مبدأ</b>" in html
    assert "<b>۲ آگهی</b> با تاریخ پرواز تا پایان فردا" in html
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
    assert chunks[0]["html"].count("<h1>") == 1
    assert all("<h1>" not in part["html"] for part in chunks[1:])
    assert all(part["html"].count("<details>") <= transfer_live.MAX_ORIGINS_PER_MESSAGE for part in chunks)
    assert all(len(chunk["html"].encode("utf-8")) <= transfer_live.MAX_RICH_MESSAGE_LENGTH for chunk in chunks)
    assert all(chunk["html"].count("<details>") == chunk["html"].count("</details>") for chunk in chunks)
    assert all(chunk["html"].count("<table ") == chunk["html"].count("</table>") for chunk in chunks)
    combined = "".join(chunk["html"] for chunk in chunks)
    assert combined.count("<td ") == 449 * 5
    assert "Moscow &amp; Dubai" in combined
    assert "بخش ۱" in combined and "بخش ۲" in combined



def test_live_report_splits_every_eight_origins_without_repeating_header(monkeypatch):
    codes = [
        "IR", "DE", "TR", "CA", "US", "GB", "FR", "IT",
        "ES", "NL", "BE", "AT", "CH", "SE", "NO", "DK",
        "FI", "PL", "GR", "RU",
    ]
    monkeypatch.setattr(
        transfer_live, "_fetch_active_rows",
        lambda: [_row(i, origin=country) for i, country in enumerate(codes, 1)],
    )
    monkeypatch.setattr(
        transfer_live, "_fetch_published_transfer_stats",
        lambda: {"last_30_days": 246},
    )
    messages = transfer_live._build_messages()
    assert [msg["html"].count("<details>") for msg in messages] == [8, 8, 4]
    assert "🟢 آگهی‌های فعال | Advertio" in messages[0]["html"]
    assert "<b>۲۰ مبدأ</b>" in messages[0]["html"]
    assert "۲۴۶" in messages[0]["html"]
    assert all(msg["html"].startswith("<details>") for msg in messages[1:])
    assert all("🟢 آگهی‌های فعال | Advertio" not in msg["html"] for msg in messages[1:])
    assert all("@advertio_bot" not in msg["html"] for msg in messages[1:])
    assert all("آخرین بروزرسانی" not in msg["html"] for msg in messages[1:])
    assert all(
        len(msg["html"].encode("utf-8")) <= transfer_live.MAX_RICH_MESSAGE_LENGTH
        for msg in messages
    )
    combined = "".join(msg["html"] for msg in messages)
    assert combined.count("<details>") == combined.count("</details>") == 20
    for i in range(1, 21):
        assert combined.count(f"<td align=\"right\">@user{i}</td>") == 1


def test_live_report_exactly_eight_origins_keeps_single_message(monkeypatch):
    codes = ["IR", "DE", "TR", "CA", "US", "GB", "FR", "IT"]
    monkeypatch.setattr(
        transfer_live, "_fetch_active_rows",
        lambda: [_row(i, origin=code) for i, code in enumerate(codes, 1)],
    )
    monkeypatch.setattr(
        transfer_live, "_fetch_published_transfer_stats",
        lambda: {"last_30_days": 0},
    )
    messages = transfer_live._build_messages()
    assert len(messages) == 1
    assert messages[0]["html"].count("<details>") == 8



def test_compact_live_header_copy_and_no_redundant_origin_label():
    markup = transfer_live._live_header(198, 20, 1034, 57)
    assert markup.startswith("<h1>🟢 آگهی‌های فعال | Advertio</h1>")
    assert "<p>📦 <b>۱۹۸ مسافر فعال</b> در <b>۲۰ مبدأ</b></p>" in markup
    assert "<p>📊 <b>۱٬۰۳۴ آگهی</b> منتشرشده در ۳۰ روز گذشته</p>" in markup
    assert "<p>⏳ <b>۵۷ آگهی</b> با تاریخ پرواز تا پایان فردا</p>" in markup
    assert '<p>📝 <b>ثبت آگهی:</b> <a href="https://t.me/advertio_bot">@advertio_bot</a></p>' in markup
    assert '<p>💬 <b>پشتیبانی:</b> <a href="https://t.me/advertio_support">@advertio_support</a></p>' in markup
    assert markup.index("⏳") < markup.index("<hr/>") < markup.index("📝")

    from datetime import date
    fragment = transfer_live._origin_table("🇦🇪 United Arab Emirates", [_row(1, origin="AE")], date(2026, 11, 11))
    summary = fragment.split("<summary>", 1)[1].split("</summary>", 1)[0]
    assert summary == "📍 از مبدا 🇦🇪 United Arab Emirates — <b>۱</b>"
    assert "آگهی" not in summary
    assert "<caption>" not in fragment
