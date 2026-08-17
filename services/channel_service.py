"""Channel/category application service.

Keeps channel configuration access out of the UI and crawler implementation.
"""

import json
from pathlib import Path


class ChannelService:
    def __init__(self, channels_file="channels.json"):
        self.channels_file = Path(channels_file)

    def load(self):
        with self.channels_file.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def categories(self):
        return list(self.load().keys())

    def channels_for_category(self, category):
        data = self.load()
        if category not in data:
            raise KeyError(f"Unknown category: {category}")
        return data[category]

    def category_with_channels(self, category):
        return {
            "category": category,
            "channels": self.channels_for_category(category),
        }
