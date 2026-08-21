"""Process-local rate and concurrency limiter for Groq Free Tier protection."""

import threading
import time


class RateLimiter:
    def __init__(self, requests_per_minute=30, min_interval_seconds=None, max_concurrency=1):
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.min_interval = (
            float(min_interval_seconds)
            if min_interval_seconds is not None
            else 60.0 / self.requests_per_minute
        )
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._concurrency = threading.BoundedSemaphore(max(1, int(max_concurrency)))

    def wait(self):
        """Throttle request start time. Concurrency is managed by slot()."""
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()

    def slot(self):
        """Acquire one in-flight request slot and release it automatically."""
        return _LimiterSlot(self._concurrency)


class _LimiterSlot:
    def __init__(self, semaphore):
        self._semaphore = semaphore

    def __enter__(self):
        self._semaphore.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._semaphore.release()
        return False
