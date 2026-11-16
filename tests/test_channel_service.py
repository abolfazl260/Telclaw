"""Tests for active and disabled channel source configuration."""

import json

from services.channel_service import ChannelService


def test_disabled_channel_is_preserved_but_not_returned_for_crawling(tmp_path):
    path = tmp_path / "channels.json"
    config = {
        "Canada_Job": [
            {"name": "Active source", "username": "active_source"},
            {
                "name": "Paused source",
                "username": "paused_source",
                "enabled": False,
            },
        ]
    }
    path.write_text(json.dumps(config), encoding="utf-8")

    service = ChannelService(path)

    assert service.categories() == ["Canada_Job"]
    assert service.channels_for_category("Canada_Job") == [
        {"name": "Active source", "username": "active_source"}
    ]
    # The disabled entry remains in the JSON for easy reactivation.
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["Canada_Job"][1]["username"] == "paused_source"
    assert stored["Canada_Job"][1]["enabled"] is False
