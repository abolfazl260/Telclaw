"""Simple process-local rate limiter for Groq Free Tier protection."""

import threading
import time


class RateLimiter:
    def __init__(self, requests_per_minute=30, min_interval_seconds=None):
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.min_interval = (
            float(min_interval_seconds)
            if min_interval_seconds is not None
            else 60.0 / self.requests_per_minute
        )
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
