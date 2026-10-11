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

    messages = transfer_live._build_messages()
    assert len(messages) == 1
    html = messages[0]["html"]
    assert "<b>2</b> آگهی در <b>1</b> مبدا" in html
    assert "<caption>2 آگهی فعال</caption>" in html
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
