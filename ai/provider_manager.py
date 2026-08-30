"""Provider selection, health tracking, and generic AI failover."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import random
import threading
import time

import config
from ai.extractor import AIExtractionError
from ai.providers.cloudflare import CloudflareProvider
from ai.providers.groq import GroqProvider

logger = logging.getLogger("telclaw.ai.failover")


@dataclass
class _ProviderState:
    identifier: str
    provider_name: str
    instance: object
    available: bool = True
    cooldown_monotonic: float = 0.0
    cooldown_until: datetime | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    failure_count: int = 0


class AIProviderManager:
    """Execute AI operations using configured credentials in priority order.

    Provider and credential health is process-local and synchronized. Network
    requests are intentionally made outside the lock, so unrelated healthy
    providers continue serving concurrent jobs.
    """

    PROVIDERS = {"groq": GroqProvider, "cloudflare": CloudflareProvider}

    def __init__(self, provider=None, provider_name=None, provider_states=None):
        self._lock = threading.RLock()
        if provider_states is not None:
            self._states = list(provider_states)
        elif provider is not None:
            name = getattr(provider, "name", getattr(provider, "provider", "custom"))
            self._states = [_ProviderState(str(name), str(name), provider)]
        else:
            self._states = self._build_states(provider_name)
        if not self._states:
            raise RuntimeError("No AI provider credentials are configured")

    def _build_states(self, provider_name):
        names = (provider_name,) if provider_name else config.AI_PROVIDERS
        states = []
        for name in names:
            normalized = name.lower()
            if normalized == "groq":
                for index, credential in enumerate(config.GROQ_PROVIDERS, 1):
                    states.append(_ProviderState(f"groq-{index}", "groq", GroqProvider([credential])))
            elif normalized == "cloudflare":
                credentials = config.CLOUDFLARE_PROVIDERS
                if not credentials:
                    # Constructing the provider gives operators a credential-specific error.
                    states.append(_ProviderState("cloudflare-1", "cloudflare", CloudflareProvider()))
                for index, credential in enumerate(credentials, 1):
                    states.append(_ProviderState(
                        f"cloudflare-{index}", "cloudflare",
                        CloudflareProvider(**credential),
                    ))
            else:
                raise RuntimeError(f"Unsupported AI provider: {normalized}")
        return states

    @property
    def provider(self):
        return self._select_available().identifier

    @property
    def model(self):
        return getattr(self._select_available().instance, "model", None)

    @property
    def provider_instance(self):
        """Compatibility accessor for callers that inspect the active provider."""
        return self._select_available().instance

    def _recover_locked(self):
        now = time.monotonic()
        for state in self._states:
            if not state.available and state.cooldown_monotonic <= now:
                state.available = True
                state.cooldown_until = None
                logger.info("[AI PROVIDER RECOVERED] provider=%s credential=%s", state.provider_name, state.identifier)

    def _select_available(self):
        with self._lock:
            self._recover_locked()
            for state in self._states:
                if state.available:
                    return state
        raise AIExtractionError("All AI providers are temporarily unavailable", reason="all_providers_unavailable")

    def _cooldown_seconds(self, state, exc, permanent=False):
        if permanent:
            return config.AI_FAILOVER_PERMANENT_COOLDOWN_SECONDS
        if exc.retry_after is not None:
            return max(0.0, float(exc.retry_after))
        exponential = config.AI_FAILOVER_BASE_COOLDOWN_SECONDS * (2 ** max(0, state.failure_count - 1))
        return min(config.AI_FAILOVER_MAX_COOLDOWN_SECONDS, exponential + random.uniform(0, min(1.0, exponential * .25)))

    @staticmethod
    def _should_failover(exc):
        if exc.status in {401, 403, 429, 502, 503, 504}:
            return True
        return exc.reason in {"network_error", "server_error", "temporary_upstream_error", "model_unavailable"}

    def _mark_failure(self, state, exc):
        permanent = exc.status in {401, 403} or exc.reason in {"invalid_api_key", "invalid_token"}
        with self._lock:
            state.failure_count += 1
            state.last_error = str(exc)[:4000]
            state.last_error_type = exc.reason or "provider_error"
            seconds = self._cooldown_seconds(state, exc, permanent=permanent)
            state.available = False
            state.cooldown_monotonic = time.monotonic() + seconds
            state.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return seconds

    def _mark_success(self, state):
        with self._lock:
            state.failure_count = 0
            state.last_error = None
            state.last_error_type = None

    def _execute(self, operation, *args):
        attempted = set()
        last_error = None
        while len(attempted) < len(self._states):
            try:
                state = self._select_available()
            except AIExtractionError:
                break
            if state.identifier in attempted:
                break
            attempted.add(state.identifier)
            try:
                result = getattr(state.instance, operation)(*args)
            except AIExtractionError as exc:
                last_error = exc
                if not self._should_failover(exc):
                    raise
                self._mark_failure(state, exc)
                try:
                    next_state = self._select_available()
                    logger.warning(
                        "[AI FAILOVER] operation=%s from=%s reason=%s to=%s",
                        operation, state.identifier, exc.reason or exc.status, next_state.identifier,
                    )
                except AIExtractionError:
                    pass
                continue
            self._mark_success(state)
            return result
        raise AIExtractionError(
            "All configured AI providers failed",
            status=getattr(last_error, "status", None),
            reason="all_providers_unavailable",
            retry_after=getattr(last_error, "retry_after", None),
        ) from last_error

    def classify_batch(self, messages):
        return self._execute("classify_batch", messages)

    def extract(self, text, category):
        return self._execute("extract", text, category)

    def get_status(self):
        with self._lock:
            self._recover_locked()
            return {
                state.identifier: {
                    "available": state.available,
                    "reason": state.last_error_type,
                    "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
                    "failure_count": state.failure_count,
                }
                for state in self._states
            }


__all__ = ["AIProviderManager"]
