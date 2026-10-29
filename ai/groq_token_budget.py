"""Non-blocking, process-wide Groq tokens-per-minute admission control.

The budget is shared across Groq keys, classification, and extraction in one
Python process. It deliberately treats the configured keys as one organization:
Groq's TPM limit is organization-level, not per API key.

Preflight uses a conservative UTF-8 byte estimate plus the configured maximum
completion tokens. Real usage and Groq's TPM response headers reconcile the
estimate. No requests are made while the shared budget is exhausted: the
existing provider manager can immediately fail over to Cloudflare.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from weakref import WeakValueDictionary

from ai.extractor import AIExtractionError


class GroqTokenBudget:
    """Thread-safe token bucket with authoritative server-header feedback."""

    def __init__(self, tokens_per_minute: int, headroom_percent: int = 10):
        if tokens_per_minute <= 0 or not 0 <= headroom_percent < 100:
            raise ValueError("Invalid Groq TPM budget")
        self.configured_limit = int(tokens_per_minute)
        self.headroom_percent = int(headroom_percent)
        self.capacity = self._safe_capacity(self.configured_limit)
        self.available = float(self.capacity)
        self._last_refill = time.monotonic()
        self._blocked_until = 0.0
        # On real Groq 429s the Requested token count may exceed our
        # tokenizer-free estimate; calibrate subsequent admission conservatively.
        self._estimate_multiplier = 1.0
        self._lock = threading.Lock()

    def _safe_capacity(self, limit):
        return max(1, int(limit * (100 - self.headroom_percent) / 100))

    @staticmethod
    def estimate_tokens(payload):
        """Conservative approximation; no remote tokenizer call or extra HTTP."""
        prompt = {
            "messages": payload.get("messages", []),
            "response_format": payload.get("response_format"),
        }
        # Persian, emojis, and JSON punctuation are represented as UTF-8
        # bytes. Three bytes/token plus 64 tokens of chat-format overhead
        # intentionally overestimates most text prompts.
        prompt_bytes = len(json.dumps(prompt, ensure_ascii=False).encode("utf-8"))
        input_tokens = math.ceil(prompt_bytes / 3) + 64
        maximum_output = max(0, int(payload.get("max_completion_tokens") or 0))
        return input_tokens + maximum_output

    @staticmethod
    def _header(headers, key):
        if not headers:
            return None
        for name, value in headers.items():
            if str(name).lower() == key:
                return value
        return None

    @staticmethod
    def _seconds(value):
        if value is None:
            return None
        text = str(value).strip().lower()
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        match = re.fullmatch(
            r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?",
            text,
        )
        if match and any(group is not None for group in match.groups()):
            hours, minutes, seconds = (float(x or 0) for x in match.groups())
            return hours * 3600 + minutes * 60 + seconds
        return None

    def _refill(self, now):
        elapsed = max(0.0, now - self._last_refill)
        self.available = min(
            float(self.capacity),
            self.available + elapsed * self.capacity / 60.0,
        )
        self._last_refill = now

    def snapshot(self):
        """Read-only monitoring view; never contacts Groq or exposes credentials.

        Available tokens are a conservative local estimate, not a live
        statement of the organization's Groq console quota.
        """
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            return {
                "configured_tpm": self.configured_limit,
                "safe_tpm": self.capacity,
                "headroom_percent": self.headroom_percent,
                "available_estimate": max(0, int(self.available)),
                "cooldown_seconds": round(max(0.0, self._blocked_until - now), 1),
                "estimate_multiplier": round(self._estimate_multiplier, 3),
            }

    def reserve(self, payload):
        """Reserve before network I/O or raise a retryable 429 for outer failover."""
        with self._lock:
            requested = math.ceil(self.estimate_tokens(payload) * self._estimate_multiplier)
            now = time.monotonic()
            self._refill(now)
            if requested > self.capacity:
                raise AIExtractionError(
                    f"Groq request estimated at {requested} tokens exceeds "
                    f"the configured safe TPM capacity ({self.capacity}); "
                    "use a smaller batch or alternate provider",
                    provider="groq",
                    reason="rate_limit",
                    status=429,
                    retry_after=60.0,
                )
            if now < self._blocked_until:
                wait = self._blocked_until - now
            elif requested > self.available:
                wait = (requested - self.available) * 60.0 / self.capacity + 0.01
            else:
                self.available -= requested
                return requested
        raise AIExtractionError(
            f"Groq shared TPM budget exhausted; requested={requested} "
            f"available={int(self.available)} capacity={self.capacity}",
            provider="groq",
            status=429,
            reason="rate_limit",
            retry_after=max(0.01, wait),
        )

    @staticmethod
    def _tpm_rejection(body):
        """Read only the numeric TPM diagnostics from a Groq 429 JSON response.

        Ignore RPM, TPD, arbitrary messages, and malformed provider output.
        """
        try:
            error = json.loads(body or "{}").get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else ""
        except (ValueError, TypeError, AttributeError):
            return None
        match = re.search(
            r"on tokens per minute\s*\(TPM\):\s*Limit\s+(\d+),\s*"
            r"Used\s+(\d+),\s*Requested\s+(\d+)",
            message,
            re.IGNORECASE,
        )
        if not match:
            return None
        limit, used, requested = (int(value) for value in match.groups())
        if limit <= 0 or used < 0 or requested <= 0:
            return None
        retry = re.search(r"try again in\s+(\d+(?:\.\d+)?)s", message, re.IGNORECASE)
        return limit, used, requested, (float(retry.group(1)) if retry else None)

    def complete(self, reserved, *, response=None, error=False):
        """Reconcile an HTTP response; on 429 no tokens should be charged.

        For transport errors, keep the reservation: the request may have reached
        Groq and consumed tokens. A later successful response replenishes via
        actual usage. This favors avoiding 429 over aggressive retransmission.
        """
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if response is None:
                return
            status = getattr(response, "status_code", 200)
            headers = getattr(response, "headers", {}) or {}

            if status == 429:
                # The denied request did not consume tokens, but the
                # organization's consumption can differ from local estimates.
                self.available = min(self.capacity, self.available + reserved)
                retry = self._seconds(self._header(headers, "retry-after"))
                rejection = self._tpm_rejection(getattr(response, "text", ""))
                if rejection is not None:
                    limit, used, provider_requested, detail_retry = rejection
                    # Groq's "Requested" is the authoritative admission size,
                    # which may exceed our UTF-8 approximation.
                    if reserved > 0:
                        self._estimate_multiplier = min(
                            4.0,
                            max(self._estimate_multiplier, provider_requested / reserved * 1.05),
                        )
                    new_capacity = self._safe_capacity(min(self.configured_limit, limit))
                    self.capacity = min(self.capacity, new_capacity)
                    self.available = min(
                        self.available,
                        max(0.0, limit - used - (limit - new_capacity)),
                    )
                    if retry is None:
                        retry = detail_retry
                if retry is not None:
                    self._blocked_until = max(self._blocked_until, now + retry)

            # A lower server-side TPM limit takes precedence over configuration.
            raw_limit = self._header(headers, "x-ratelimit-limit-tokens")
            try:
                server_limit = int(raw_limit) if raw_limit is not None else None
            except (ValueError, TypeError):
                server_limit = None
            if server_limit and server_limit > 0:
                new_capacity = self._safe_capacity(min(self.configured_limit, server_limit))
                if new_capacity < self.capacity:
                    self.capacity = new_capacity
                    self.available = min(self.available, float(self.capacity))

            raw_remaining = self._header(headers, "x-ratelimit-remaining-tokens")
            try:
                remaining = float(raw_remaining) if raw_remaining is not None else None
            except (ValueError, TypeError):
                remaining = None
            if remaining is not None and math.isfinite(remaining):
                # Keep the configured headroom even when provider reports tokens
                # as technically available.
                server_cap = server_limit if server_limit and server_limit > 0 else self.configured_limit
                safety_tokens = server_cap - self._safe_capacity(server_cap)
                self.available = min(
                    self.available,
                    max(0.0, remaining - safety_tokens),
                )

    def reconcile_usage(self, reserved, data):
        """Replace a conservative reservation with actual billable usage."""
        if not isinstance(data, dict):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        try:
            actual = float(usage["total_tokens"])
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            actual -= max(0.0, float(cached or 0))
        except (KeyError, ValueError, TypeError, AttributeError):
            return
        if not math.isfinite(actual) or actual < 0:
            return
        with self._lock:
            self._refill(time.monotonic())
            self.available = max(
                0.0, min(float(self.capacity), self.available + reserved - actual)
            )


_REGISTRY_LOCK = threading.Lock()
_BUDGETS: WeakValueDictionary[tuple[int, int], GroqTokenBudget] = WeakValueDictionary()


def shared_groq_token_budget(tokens_per_minute: int, headroom_percent: int):
    """Share one conservative organization-level budget per process.

    If multiple Groq organizations are configured in one process, they are
    conservatively treated as one budget rather than risking overuse.
    """
    key = (int(tokens_per_minute), int(headroom_percent))
    with _REGISTRY_LOCK:
        budget = _BUDGETS.get(key)
        if budget is None:
            budget = GroqTokenBudget(*key)
            _BUDGETS[key] = budget
        return budget


def peek_groq_token_budget(tokens_per_minute: int, headroom_percent: int):
    """Return an existing local budget without creating/resetting quota state."""
    key = (int(tokens_per_minute), int(headroom_percent))
    with _REGISTRY_LOCK:
        return _BUDGETS.get(key)
