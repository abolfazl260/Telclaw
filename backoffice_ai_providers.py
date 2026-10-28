"""Safe, passive AI provider availability snapshot for Back Office Settings.

This does not initialize AI clients, test credentials, or call any external API.
It reports currently configured routing and process-local, *observed* state.
No secrets, credential identifiers or raw provider responses are exposed.
"""

from __future__ import annotations

import time

import config
from ai.groq_token_budget import peek_groq_token_budget


def _running_managers(operations):
    managers = {}
    if operations is None:
        return managers
    for label, attr in (("Extraction", "_ai"), ("Classification", "_classification")):
        service = getattr(operations, attr, None)
        manager = getattr(service, "provider_manager", None)
        if manager is not None:
            managers[label] = manager
    return managers


def _seconds(value, now):
    return round(max(0.0, float(value or 0) - now), 1)


def snapshot(operations=None):
    """Return provider status without changing routing or making HTTP calls."""
    now = time.monotonic()
    names = tuple(config.AI_PROVIDERS)
    managers = _running_managers(operations)
    routing = {}
    for stage, manager in managers.items():
        items = list(getattr(manager, "providers", ()))
        if not items:
            continue
        configured_names = tuple(getattr(item, "name", "") for item in items)
        index = getattr(manager, "active_index", 0)
        if not isinstance(index, int) or not 0 <= index < len(items):
            continue
        routing[stage] = {
            "selected": configured_names[index],
            "stale_config": configured_names != names,
        }

    budget = peek_groq_token_budget(
        config.GROQ_TOKENS_PER_MINUTE, config.GROQ_TPM_HEADROOM_PERCENT
    )
    token_snapshot = budget.snapshot() if budget is not None else None

    result = []
    for name in ("groq", "cloudflare"):
        credentials = (
            list(config.GROQ_PROVIDERS) if name == "groq"
            else list(config.CLOUDFLARE_PROVIDERS)
        )
        enabled = name in names
        observed = []
        for manager in managers.values():
            for idx, provider in enumerate(getattr(manager, "providers", ())):
                if getattr(provider, "name", "") != name:
                    continue
                until = getattr(manager, "_unavailable_until", ())
                manager_wait = _seconds(until[idx], now) if idx < len(until) else 0.0
                provider_deadlines = getattr(provider, "_unavailable_until", ())
                account_waits = [
                    _seconds(deadline, now) for deadline in provider_deadlines
                ]
                observed.append((manager_wait, account_waits))

        account_waits = [0.0] * len(credentials)
        outer_wait = 0.0
        for manager_wait, waits in observed:
            outer_wait = max(outer_wait, manager_wait)
            for idx, wait in enumerate(waits):
                if idx < len(account_waits):
                    account_waits[idx] = max(account_waits[idx], wait)

        accounts = [
            {
                "label": f"{'Key' if name == 'groq' else 'Account'} {i + 1}",
                "model": str(item.get("model") or ""),
                "cooldown_seconds": account_waits[i],
            }
            for i, item in enumerate(credentials)
        ]
        any_eligible_account = any(wait <= 0 for wait in account_waits)
        blocked = bool(observed) and (outer_wait > 0 or not any_eligible_account)
        if not enabled:
            state = "Disabled"
        elif not credentials:
            state = "Missing credentials"
        elif blocked:
            state = "Cooldown"
        elif observed:
            state = "Eligible (not health-checked)"
        else:
            state = "Configured (not yet observed)"

        selected_for = [
            stage for stage, route in routing.items() if route["selected"] == name
        ]
        token_info = None
        if name == "groq":
            token_info = {
                "source": "process-local estimate" if token_snapshot else "not observed",
                "configured_tpm": int(config.GROQ_TOKENS_PER_MINUTE),
                "safe_tpm": (token_snapshot["safe_tpm"] if token_snapshot else
                             max(1, int(config.GROQ_TOKENS_PER_MINUTE *
                                 (100 - config.GROQ_TPM_HEADROOM_PERCENT) / 100))),
                "available_estimate": (token_snapshot["available_estimate"] if token_snapshot else None),
                "cooldown_seconds": (token_snapshot["cooldown_seconds"] if token_snapshot else None),
            }
        else:
            token_info = {
                "source": "not reported by current integration",
                "configured_tpm": None,
                "safe_tpm": None,
                "available_estimate": None,
                "cooldown_seconds": None,
            }
        result.append({
            "name": name,
            "priority": names.index(name) + 1 if enabled else None,
            "enabled": enabled,
            "credential_count": len(credentials),
            "state": state,
            "selected_for": selected_for,
            "observed": bool(observed),
            "cooldown_seconds": outer_wait,
            "accounts": accounts,
            "tokens": token_info,
        })

    return {
        "providers": result,
        "routes": routing,
        "updated_at": time.time(),
        "scope": "This process only; external accounts and other processes are not monitored.",
        "requires_restart": any(route["stale_config"] for route in routing.values()),
    }
