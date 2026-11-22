"""Channel/category application service.

Keeps channel configuration access out of the UI and crawler implementation.
"""

import json
from pathlib import Path
import os
import re
import tempfile
from threading import Lock

_INVENTORY_LOCK = Lock()


class ChannelService:
    def __init__(self, channels_file="channels.json"):
        self.channels_file = Path(channels_file)

    def load(self):
        with self.channels_file.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def enroll(self, category, username):
        username = str(username).strip().lstrip('@')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
            raise ValueError('A public source username is required')
        with _INVENTORY_LOCK:
            data = self.load()
            if category not in data:
                raise ValueError('Choose an existing crawler category')
            row = next((r for r in data[category] if isinstance(r, dict) and str(r.get('username','')).lstrip('@').casefold() == username.casefold()), None)
            if row is None:
                data[category].append({'username': username, 'enabled': True})
            else:
                row['enabled'] = True
            path = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.channels_file.parent, delete=False) as handle:
                    path = handle.name
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(path, self.channels_file)
            finally:
                if path and os.path.exists(path): os.unlink(path)

    def categories(self):
        return list(self.load().keys())

    def channels_for_category(self, category):
        data = self.load()
        if category not in data:
            raise KeyError(f"Unknown category: {category}")
        # JSON has no comment syntax; an explicit enabled=false preserves a
        # source entry while excluding it from every crawler scheduling path.
        return [
            channel for channel in data[category]
            if not (isinstance(channel, dict) and channel.get("enabled") is False)
        ]

    def category_with_channels(self, category):
        return {
            "category": category,
            "channels": self.channels_for_category(category),
        }
