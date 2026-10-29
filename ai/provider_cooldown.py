"""Process-wide cooldown state shared by AI pipeline stages.

Classification and extraction each own an AIProviderManager, and each manager
creates its own provider clients.  Shared deadlines prevent one stage from
immediately calling a provider/credential that another stage has cooled down.
This is deliberately process-local; no credentials or API responses are stored.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from weakref import WeakValueDictionary


# Only stable diagnostic codes are kept; never persist exception text, provider
# response bodies, API keys, model IDs, or credentials in shared cooldown state.
_SAFE_CAUSES = frozenset({
    "rate_limit", "tpm_limit", "rpm_limit", "daily_quota", "local_tpm_budget",
    "network_error", "server_error", "permissions_error", "invalid_api_key",
    "model_blocked", "model_not_found", "provider_error", "unknown",
})


def cooldown_cause(error):
    """Map an AI error to one safe, operator-readable cooldown code."""
    specific = getattr(error, "cooldown_cause", None)
    if specific in _SAFE_CAUSES:
        return specific
    reason = getattr(error, "reason", None)
    return reason if reason in _SAFE_CAUSES else "unknown"


class CooldownState:
    def __init__(self, size: int):
        self.unavailable_until = [0.0] * size
        self._causes = [None] * size
        self._started_at = [None] * size
        self._lock = threading.Lock()

    def block(self, index: int, seconds: float, *, cause=None) -> float:
        """Extend (never shorten) the deadline and attribute its cause.

        If a shorter event occurs while already cooling, keep the cause of the
        deadline actually preventing new requests, not the last incidental error.
        """
        wait = max(0.0, float(seconds))
        deadline = time.monotonic() + wait
        with self._lock:
            if deadline > self.unavailable_until[index] and wait > 0:
                self.unavailable_until[index] = deadline
                self._causes[index] = cause if cause in _SAFE_CAUSES else "unknown"
                self._started_at[index] = datetime.now(timezone.utc).isoformat()
            return self.unavailable_until[index]

    def snapshot(self):
        """Atomically read diagnostic metadata without changing routing state."""
        with self._lock:
            return [
                {"deadline": deadline, "cause": cause, "at": at}
                for deadline, cause, at in zip(
                    self.unavailable_until, self._causes, self._started_at
                )
            ]


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
