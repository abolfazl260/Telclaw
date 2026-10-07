from monitoring.transfer_live import _build_country_messages, _country_summary_rows


def test_country_summary_groups_active_rows_by_origin_and_destination():
    rows = [
        {"origin_country": "IR", "destination_country": "DE"},
        {"origin_country": "ir", "destination_country": "de"},
        {"origin_country": "TR", "destination_country": "DE"},
        {"origin_country": "IR", "destination_country": "CA"},
        {"origin_country": "", "destination_country": "CA"},
        {"origin_country": "IR", "destination_country": ""},
    ]

    assert _country_summary_rows(rows) == [
        (("IR", "CA"), 1),
        (("IR", "DE"), 2),
        (("TR", "DE"), 1),
    ]


def test_country_rich_message_contains_country_counts(monkeypatch):
    monkeypatch.setattr(
        "monitoring.transfer_live._fetch_active_country_summary",
        lambda: [(("IR", "DE"), 2), (("TR", "CA"), 1)],
    )

    message = _build_country_messages()[0]["html"]

    assert "Advertio Cargo &amp; Passenger Requests" in message
    assert "@advertio_cargo" in message
    assert "Total active requests: <b>3</b>" in message
    assert "Latest published flight number: <b>0</b>" in message
    assert "Published in the last 7 days: <b>0</b>" in message
    assert "Published in the last 30 days: <b>0</b>" in message
    assert "Origin" in message
    assert "Destination" in message
    assert "Iran 🇮🇷" in message
    assert "Germany 🇩🇪" in message
    assert "Turkey 🇹🇷" in message
    assert "Canada 🇨🇦" in message
    assert ">2<" in message
    assert ">1<" in message
