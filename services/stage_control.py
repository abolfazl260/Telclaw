"""Thread-safe operator controls for skipping the current pipeline stage."""
from __future__ import annotations

import threading


STAGES = ("crawl", "processing", "ai", "advertio")


class StageControl:
    """One-shot skip requests shared by the scheduler, async crawler and workers."""

    def __init__(self):
        self._events = {stage: threading.Event() for stage in STAGES}
        self._lock = threading.Lock()

    def request_skip(self, stage: str) -> bool:
        if stage not in self._events:
            return False
        self._events[stage].set()
        return True

    def is_skip_requested(self, stage: str) -> bool:
        event = self._events.get(stage)
        return bool(event and event.is_set())

    def consume_skip(self, stage: str) -> bool:
        event = self._events.get(stage)
        if event is None:
            return False
        was_set = event.is_set()
        if was_set:
            event.clear()
        return was_set

    def clear(self, stage: str) -> None:
        event = self._events.get(stage)
        if event:
            event.clear()


_stage_control = StageControl()


def get_stage_control() -> StageControl:
    return _stage_control
