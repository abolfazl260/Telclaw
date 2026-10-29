"""Back Office provider monitoring is read-only, safe and explicit about unknowns."""

import json
import time
from types import SimpleNamespace

import pytest

import config
import backoffice_ai_providers
import backoffice_web
from ai.groq_token_budget import shared_groq_token_budget


@pytest.fixture
def provider_config(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDERS", ("groq", "cloudflare"))
    monkeypatch.setattr(config, "GROQ_PROVIDERS", [
        {"api_key": "secret-groq-primary", "model": "model-a"},
        {"api_key": "secret-groq-secondary", "model": "model-b"},
    ])
    monkeypatch.setattr(config, "CLOUDFLARE_PROVIDERS", [
        {"account_id": "hidden-account", "api_token": "secret-cloudflare-token",
         "model": "@cf/test/model"},
    ])
    monkeypatch.setattr(config, "GROQ_TOKENS_PER_MINUTE", 4237)
    monkeypatch.setattr(config, "GROQ_TPM_HEADROOM_PERCENT", 10)


def _manager(*, groq_wait=0.0, cloudflare_wait=0.0, active=0):
    groq = SimpleNamespace(name="groq", _unavailable_until=[groq_wait, 0.0])
    cloudflare = SimpleNamespace(name="cloudflare", _unavailable_until=[cloudflare_wait])
    return SimpleNamespace(
        providers=[groq, cloudflare], active_index=active,
        _unavailable_until=[groq_wait, cloudflare_wait],
    )


def test_non_observed_token_balance_is_unknown_not_full(provider_config):
    result = backoffice_ai_providers.snapshot()
    groq, cloudflare = result["providers"]
    assert groq["name"] == "groq"
    assert groq["priority"] == 1
    assert groq["state"] == "Configured (not yet observed)"
    assert groq["credential_count"] == 2
    assert groq["selected_for"] == []
    assert groq["tokens"]["available_estimate"] is None
    assert groq["tokens"]["safe_tpm"] == 3813
    assert cloudflare["priority"] == 2
    assert cloudflare["tokens"]["available_estimate"] is None
    assert cloudflare["tokens"]["source"] == "not reported by current integration"


def test_real_local_budget_is_read_without_refilling_to_full(provider_config):
    budget = shared_groq_token_budget(4237, 10)
    payload = {"messages": [{"role": "user", "content": "one two three"}],
               "max_completion_tokens": 200}
    reserved = budget.reserve(payload)
    result = backoffice_ai_providers.snapshot()
    tokens = result["providers"][0]["tokens"]
    assert tokens["source"] == "process-local estimate"
    assert 0 < tokens["available_estimate"] < tokens["safe_tpm"]
    assert tokens["safe_tpm"] == 3813
    assert reserved > 0


def test_cooldown_and_stage_selection_are_visible(provider_config):
    until = time.monotonic() + 25
    operations = SimpleNamespace(
        _ai=SimpleNamespace(provider_manager=_manager(groq_wait=until, active=1)),
        _classification=SimpleNamespace(provider_manager=_manager(groq_wait=until, active=1)),
    )
    result = backoffice_ai_providers.snapshot(operations)
    groq, cloudflare = result["providers"]
    assert groq["state"] == "Cooldown"
    assert 0 < groq["cooldown_seconds"] <= 25
    assert groq["accounts"][0]["cooldown_seconds"] > 0
    assert groq["selected_for"] == []
    assert cloudflare["state"] == "Eligible (not health-checked)"
    assert cloudflare["selected_for"] == ["Extraction", "Classification"]
    assert not result["requires_restart"]


def test_disabled_and_missing_credentials_are_distinct(provider_config, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDERS", ("cloudflare",))
    result = backoffice_ai_providers.snapshot()
    assert result["providers"][0]["state"] == "Disabled"
    monkeypatch.setattr(config, "CLOUDFLARE_PROVIDERS", [])
    result = backoffice_ai_providers.snapshot()
    assert result["providers"][1]["state"] == "Missing credentials"


def test_stale_provider_priority_warns_restart(provider_config):
    operations = SimpleNamespace(
        _ai=SimpleNamespace(provider_manager=_manager()),
        _classification=None,
    )
    config.AI_PROVIDERS = ("cloudflare", "groq")
    assert backoffice_ai_providers.snapshot(operations)["requires_restart"]


def test_secret_never_present_in_snapshot_or_html(provider_config):
    data = backoffice_ai_providers.snapshot()
    from backoffice_ai_providers_ui import render
    combined = json.dumps(data) + render(data, "safe-nonce")
    for secret in ("secret-groq-primary", "secret-groq-secondary",
                   "secret-cloudflare-token", "hidden-account"):
        assert secret not in combined
    assert "AI Provider Status" in combined
    assert "estimated" in combined or "Unknown" in combined
    assert "/settings/ai-providers/status" in combined
    assert 'nonce="safe-nonce"' in combined


@pytest.mark.asyncio
async def test_settings_page_and_json_route_render_status(provider_config, monkeypatch):
    monkeypatch.setattr(backoffice_web.backoffice_settings, "rows", lambda: [])

    class Request(dict):
        app = {"operations": SimpleNamespace(_ai=None, _classification=None)}
        query = {}

    request = Request(session={"csrf": "safe-csrf"}, csp_nonce="safe-nonce")
    page = await backoffice_web.settings_page(request)
    assert page.status == 200
    assert "AI Provider Status" in page.text
    assert "Groq" in page.text
    assert "Cloudflare" in page.text

    response = await backoffice_web.ai_provider_status(request)
    data = json.loads(response.body)
    assert data["providers"][0]["name"] == "groq"
    assert data["providers"][1]["name"] == "cloudflare"


def test_status_route_is_registered(provider_config, monkeypatch, tmp_path):
    from storage import database
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "providers.sqlite"))
    database.initialize_db()
    app = backoffice_web.create_app()
    routes = {route.resource.canonical for route in app.router.routes()}
    assert "/settings/ai-providers/status" in routes
    assert "/settings" in routes
