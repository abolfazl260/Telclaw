"""Process-wide cooldown state shared by AI pipeline stages.

Classification and extraction each own an AIProviderManager, and each manager
creates its own provider clients.  Shared deadlines prevent one stage from
immediately calling a provider/credential that another stage has cooled down.
This is deliberately process-local; no credentials or API responses are stored.
"""

from __future__ import annotations

import threading
import time
from weakref import WeakValueDictionary


class CooldownState:
    def __init__(self, size: int):
        self.unavailable_until = [0.0] * size
        self._lock = threading.Lock()

    def block(self, index: int, seconds: float) -> float:
        """Extend (never shorten) a deadline, including during concurrent calls."""
        deadline = time.monotonic() + max(0.0, float(seconds))
        with self._lock:
            self.unavailable_until[index] = max(
                self.unavailable_until[index], deadline
            )
            return self.unavailable_until[index]


_LOCK = threading.Lock()
_STATES: WeakValueDictionary[tuple, CooldownState] = WeakValueDictionary()


def shared_cooldowns(scope: str, identities) -> CooldownState:
    """Reuse deadlines while at least one client retains the state.

    Pool identity includes the ordered set of configured providers/accounts.
    Injected test providers intentionally use private state instead.
    """
    identities = tuple(identities)
    key = (scope, identities)
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            state = CooldownState(len(identities))
            _STATES[key] = state
        return state
