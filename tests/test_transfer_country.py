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
    assert "@Advertio_cargo" in message
    assert "Total open requests: <b>3</b>" in message
    assert "Total published ads: <b>0</b>" in message
    assert "Origin" in message
    assert "Destination" in message
    assert "ایران" in message
    assert "آلمان" in message
    assert "ترکیه" in message
    assert "کانادا" in message
    assert ">2<" in message
    assert ">1<" in message
