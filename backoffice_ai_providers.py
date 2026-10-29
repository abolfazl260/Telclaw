"""Passive, secret-free AI provider and cooldown diagnostics for Back Office.

No network calls or client initialization. Every numeric token value is a
process-local approximation; provider/account eligibility is independent of
the displayed Groq token bucket.
"""
from __future__ import annotations

import time

import config
from ai.groq_token_budget import peek_groq_token_budget


CAUSE_LABELS = {
    "rate_limit": "Rate limit (scope unknown)",
    "tpm_limit": "Groq tokens-per-minute (TPM)",
    "rpm_limit": "Requests-per-minute (RPM)",
    "daily_quota": "Daily quota",
    "local_tpm_budget": "Local Groq TPM admission budget",
    "network_error": "Network error",
    "server_error": "Provider server error",
    "permissions_error": "Permission denied",
    "invalid_api_key": "Invalid API key",
    "model_blocked": "Model access blocked",
    "model_not_found": "Model not found",
    "provider_error": "Provider error",
    "unknown": "Unknown (not recorded)",
}


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


def _cooldown_rows(owner, now, count):
    """Prefer atomic metadata snapshots; support old/simple injected providers."""
    state = getattr(owner, "_cooldowns", None)
    snap = getattr(state, "snapshot", None)
    entries = snap() if callable(snap) else ()
    deadlines = getattr(owner, "_unavailable_until", ())
    result = []
    for index in range(count):
        item = entries[index] if index < len(entries) else {}
        deadline = item.get("deadline", deadlines[index] if index < len(deadlines) else 0)
        cause = item.get("cause")
        result.append({
            "wait": _seconds(deadline, now),
            "cause": cause if cause in CAUSE_LABELS else None,
            "at": item.get("at"),
        })
    return result


def _last_cooldown(rows):
    """Show the cause of the current block, else the most recent past block."""
    blocked = [row for row in rows if row["wait"] > 0]
    candidates = [row for row in blocked if row["at"]] or [
        row for row in rows if row["at"]
    ]
    if candidates:
        event = max(candidates, key=lambda row: row["at"])
        cause, at = event["cause"], event["at"]
    elif blocked:
        cause, at = None, None
    else:
        return None
    return {
        "code": cause or "unknown",
        "label": CAUSE_LABELS.get(cause or "unknown", CAUSE_LABELS["unknown"]),
        "at": at,
        "active": bool(blocked),
    }


def snapshot(operations=None):
    """Read current routing, cooldown provenance and local budget only."""
    now = time.monotonic()
    names = tuple(config.AI_PROVIDERS)
    managers = _running_managers(operations)
    routing = {}
    for stage, manager in managers.items():
        items = list(getattr(manager, "providers", ()))
        index = getattr(manager, "active_index", 0)
        if not items or not isinstance(index, int) or not 0 <= index < len(items):
            continue
        configured_names = tuple(getattr(item, "name", "") for item in items)
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
            provider_items = getattr(manager, "providers", ())
            for index, provider in enumerate(provider_items):
                if getattr(provider, "name", "") != name:
                    continue
                manager_state = _cooldown_rows(manager, now, len(provider_items))
                provider_state = _cooldown_rows(
                    provider, now, len(getattr(provider, "providers", credentials))
                )
                observed.append((manager_state[index], provider_state))

        account_states = [[] for _ in credentials]
        provider_states = []
        for manager_state, provider_state in observed:
            provider_states.append(manager_state)
            for index, entry in enumerate(provider_state):
                if index < len(account_states):
                    account_states[index].append(entry)

        account_waits = [
            max((entry["wait"] for entry in entries), default=0.0)
            for entries in account_states
        ]
        outer_wait = max((entry["wait"] for entry in provider_states), default=0.0)
        eligible_accounts = any(wait <= 0 for wait in account_waits)
        credential_wait = (min(account_waits) if account_waits and not eligible_accounts else 0.0)
        budget_wait = (
            token_snapshot["cooldown_seconds"]
            if name == "groq" and token_snapshot is not None else 0.0
        )
        blocked = bool(observed) and (
            outer_wait > 0 or not eligible_accounts or budget_wait > 0
        )
        effective_wait = max(outer_wait, credential_wait, budget_wait) if blocked else 0.0

        accounts = [
            {
                "label": f"{'Key' if name == 'groq' else 'Account'} {index + 1}",
                "model": str(item.get("model") or ""),
                "cooldown_seconds": account_waits[index],
                "last_cooldown": _last_cooldown(account_states[index]),
            }
            for index, item in enumerate(credentials)
        ]
        last_cooldown = _last_cooldown(
            provider_states + [entry for entries in account_states for entry in entries]
        )
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
        if name == "groq":
            tokens = {
                "source": "process-local estimate" if token_snapshot else "not observed",
                "configured_tpm": int(config.GROQ_TOKENS_PER_MINUTE),
                "safe_tpm": (token_snapshot["safe_tpm"] if token_snapshot else
                             max(1, int(config.GROQ_TOKENS_PER_MINUTE *
                                 (100 - config.GROQ_TPM_HEADROOM_PERCENT) / 100))),
                "available_estimate": (token_snapshot["available_estimate"] if token_snapshot else None),
                "cooldown_seconds": (token_snapshot["cooldown_seconds"] if token_snapshot else None),
                "usable_now": bool(enabled and observed and credentials and not blocked),
            }
        else:
            tokens = {
                "source": "not reported by current integration",
                "configured_tpm": None, "safe_tpm": None,
                "available_estimate": None, "cooldown_seconds": None,
                "usable_now": None,
            }
        result.append({
            "name": name,
            "priority": names.index(name) + 1 if enabled else None,
            "enabled": enabled,
            "credential_count": len(credentials),
            "state": state,
            "selected_for": selected_for,
            "selection_label": (
                ", ".join(selected_for) if selected_for
                else "Not selected" if observed else "Not observed"
            ),
            "observed": bool(observed),
            "cooldown_seconds": effective_wait,
            "last_cooldown": last_cooldown,
            "accounts": accounts,
            "tokens": tokens,
        })

    return {
        "providers": result, "routes": routing, "updated_at": time.time(),
        "scope": "This process only; other workers and hosts are not monitored.",
        "requires_restart": any(route["stale_config"] for route in routing.values()),
    }
