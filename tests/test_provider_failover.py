import os

import pytest

# config.py validates required runtime settings at import time. These values
# are test-only placeholders and are never used for network requests.
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("GROQ_API_KEY", "test-primary-key")
os.environ.setdefault("TELCLAW_GROQ_MODEL", "test-model")

from ai.extractor import AIExtractionError
from ai.provider_failover import GroqProviderFailover


def _providers():
    return [
        {"api_key": "key-1", "model": "model-1"},
        {"api_key": "key-2", "model": "model-2"},
        {"api_key": "key-3", "model": "model-3"},
    ]


def _error(wait, reason="rate_limit"):
    return AIExtractionError(
        "rate limited",
        status=429,
        reason=reason,
        retry_after=wait,
    )


def test_success_uses_api_one():
    failover = GroqProviderFailover(_providers())
    calls = []

    failover.providers[0].extract = lambda text: calls.append(1) or {"ok": True}

    assert failover.extract("message") == {"ok": True}
    assert calls == [1]
    assert failover.active_index == 0


def test_short_rate_limit_does_not_switch():
    failover = GroqProviderFailover(_providers())
    failover.providers[0].extract = lambda text: (_ for _ in ()).throw(_error(200))

    with pytest.raises(AIExtractionError) as raised:
        failover.extract("message")

    assert raised.value.retry_after == 200
    assert failover.active_index == 0


def test_wait_over_200_switches_to_api_two():
    failover = GroqProviderFailover(_providers())
    failover.providers[0].extract = lambda text: (_ for _ in ()).throw(_error(201))
    failover.providers[1].extract = lambda text: {"provider": 2}

    assert failover.extract("message") == {"provider": 2}
    assert failover.active_index == 1


def test_api_two_can_fail_over_to_api_three():
    failover = GroqProviderFailover(_providers())
    failover.providers[0].extract = lambda text: (_ for _ in ()).throw(_error(201))
    failover.providers[1].extract = lambda text: (_ for _ in ()).throw(_error(300))
    failover.providers[2].extract = lambda text: {"provider": 3}

    assert failover.extract("message") == {"provider": 3}
    assert failover.active_index == 2


def test_non_rate_limit_error_does_not_switch():
    failover = GroqProviderFailover(_providers())
    failover.providers[0].extract = lambda text: (_ for _ in ()).throw(
        AIExtractionError("bad request", status=400, reason="provider_error")
    )

    with pytest.raises(AIExtractionError) as raised:
        failover.extract("message")

    assert raised.value.status == 400
    assert failover.active_index == 0


def test_recovered_api_one_regains_priority():
    failover = GroqProviderFailover(_providers())
    failover.providers[0].extract = lambda text: (_ for _ in ()).throw(_error(201))
    failover.providers[1].extract = lambda text: {"provider": 2}

    assert failover.extract("message") == {"provider": 2}
    assert failover.active_index == 1

    failover._unavailable_until[0] = 0
    failover.providers[0].extract = lambda text: {"provider": 1}

    assert failover.extract("message") == {"provider": 1}
    assert failover.active_index == 0
