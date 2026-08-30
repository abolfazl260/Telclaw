import importlib

import pytest

import config
from ai.provider_manager import AIProviderManager
from ai.providers.factory import create_provider


def _reload(monkeypatch, **values):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test-hash")
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return importlib.reload(config)


def test_invalid_provider_fails_with_clear_error(monkeypatch):
    with pytest.raises(RuntimeError, match="Invalid AI provider"):
        _reload(monkeypatch, AI_PROVIDER_1="not-a-provider", AI_PROVIDER_2=None)


def test_missing_groq_credentials_fail_provider_initialization(monkeypatch):
    _reload(monkeypatch, AI_PROVIDER_1="groq", AI_PROVIDER_2=None, GROQ_API_KEY=None, GROQ_API_KEY_2=None, GROQ_API_KEY_3=None, TELCLAW_GROQ_MODEL=None)
    with pytest.raises(RuntimeError, match="No Groq AI providers are configured"):
        create_provider("groq")


def test_multiple_groq_credentials_are_preserved_in_priority_order(monkeypatch):
    cfg = _reload(
        monkeypatch,
        AI_PROVIDER_1="groq",
        AI_PROVIDER_2=None,
        GROQ_API_KEY="key-one",
        GROQ_API_KEY_2="key-two",
        GROQ_API_KEY_3="key-three",
        TELCLAW_GROQ_MODEL="model-one",
        TELCLAW_GROQ_MODEL_2=None,
        TELCLAW_GROQ_MODEL_3=None,
    )
    assert [item["api_key"] for item in cfg.GROQ_PROVIDERS] == ["key-one", "key-two", "key-three"]
    provider = create_provider("groq")
    assert len(provider.providers) == 3


def test_backward_compatible_groq_only_configuration(monkeypatch):
    _reload(
        monkeypatch,
        AI_PROVIDER_1="groq",
        AI_PROVIDER_2=None,
        GROQ_API_KEY="legacy-key",
        GROQ_API_KEY_2=None,
        GROQ_API_KEY_3=None,
        TELCLAW_GROQ_MODEL="legacy-model",
    )
    provider = create_provider("groq")
    manager = AIProviderManager(provider=provider)
    assert manager.provider == "groq-1"
    assert manager.model == "legacy-model"
    status = manager.get_status()
    assert "legacy-key" not in str(status)
    assert status["configured_credentials"] == [1]


def test_single_cloudflare_credential(monkeypatch):
    cfg = _reload(
        monkeypatch,
        AI_PROVIDER_1="cloudflare",
        AI_PROVIDER_2=None,
        CLOUDFLARE_ACCOUNT_ID="account-one",
        CLOUDFLARE_API_TOKEN="token-one",
        CLOUDFLARE_MODEL="model-one",
        CLOUDFLARE_ACCOUNT_ID_2="",
        CLOUDFLARE_API_TOKEN_2="",
        CLOUDFLARE_MODEL_2="",
        CLOUDFLARE_ACCOUNT_ID_3="",
        CLOUDFLARE_API_TOKEN_3="",
        CLOUDFLARE_MODEL_3="",
    )
    assert cfg.CLOUDFLARE_PROVIDERS == [{"account_id": "account-one", "api_token": "token-one", "model": "model-one"}]
    assert len(create_provider("cloudflare").providers) == 1


def test_multiple_cloudflare_credentials(monkeypatch):
    cfg = _reload(
        monkeypatch,
        AI_PROVIDER_1="cloudflare",
        AI_PROVIDER_2=None,
        CLOUDFLARE_ACCOUNT_ID="account-one",
        CLOUDFLARE_API_TOKEN="token-one",
        CLOUDFLARE_MODEL="model-one",
        CLOUDFLARE_ACCOUNT_ID_2="account-two",
        CLOUDFLARE_API_TOKEN_2="token-two",
        CLOUDFLARE_MODEL_2="model-two",
    )
    assert cfg.CLOUDFLARE_PROVIDERS == [
        {"account_id": "account-one", "api_token": "token-one", "model": "model-one"},
        {"account_id": "account-two", "api_token": "token-two", "model": "model-two"},
    ]


def test_empty_optional_cloudflare_slots_are_ignored_even_with_default_model(monkeypatch):
    cfg = _reload(
        monkeypatch,
        AI_PROVIDER_1="cloudflare",
        AI_PROVIDER_2=None,
        CLOUDFLARE_ACCOUNT_ID="account-one",
        CLOUDFLARE_API_TOKEN="token-one",
        CLOUDFLARE_MODEL="model-one",
        CLOUDFLARE_ACCOUNT_ID_2="",
        CLOUDFLARE_API_TOKEN_2="",
        CLOUDFLARE_MODEL_2="",
        CLOUDFLARE_ACCOUNT_ID_3="",
        CLOUDFLARE_API_TOKEN_3="",
        CLOUDFLARE_MODEL_3="",
    )
    assert len(cfg.CLOUDFLARE_PROVIDERS) == 1


@pytest.mark.parametrize(
    "slot,values,expected_missing",
    [
        (2, {"CLOUDFLARE_ACCOUNT_ID_2": "account-two", "CLOUDFLARE_API_TOKEN_2": "", "CLOUDFLARE_MODEL_2": "model-two"}, "CLOUDFLARE_API_TOKEN_2"),
        (3, {"CLOUDFLARE_ACCOUNT_ID_3": "", "CLOUDFLARE_API_TOKEN_3": "token-three", "CLOUDFLARE_MODEL_3": "model-three"}, "CLOUDFLARE_ACCOUNT_ID_3"),
    ],
)
def test_partial_cloudflare_credential_configuration_fails(monkeypatch, slot, values, expected_missing):
    with pytest.raises(RuntimeError, match=expected_missing):
        _reload(
            monkeypatch,
            AI_PROVIDER_1="cloudflare",
            AI_PROVIDER_2=None,
            CLOUDFLARE_ACCOUNT_ID="account-one",
            CLOUDFLARE_API_TOKEN="token-one",
            CLOUDFLARE_MODEL="model-one",
            **values,
        )
