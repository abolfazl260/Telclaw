import copy
import os

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("TELCLAW_GROQ_MODEL", "test-model")

import pytest

import backoffice_settings
import config
from storage import database


@pytest.fixture
def settings_db(tmp_path, monkeypatch):
    tracked = {
        spec.attr for spec in backoffice_settings.SPECS
        if spec.attr is not None
    } | {
        "AI_PROVIDERS", "GROQ_PROVIDERS", "CLOUDFLARE_PROVIDERS",
        "AI_EXTRACTION_CATEGORY_ENABLED",
    }
    snapshot = {
        name: copy.deepcopy(getattr(config, name))
        for name in tracked if hasattr(config, name)
    }

    path = tmp_path / "settings.sqlite"
    monkeypatch.setattr(config, "DB_NAME", str(path))
    database.initialize_db()
    backoffice_settings.initialize()
    try:
        yield path
    finally:
        for name, value in snapshot.items():
            setattr(config, name, value)


def test_env_is_default_and_override_wins(settings_db, monkeypatch):
    monkeypatch.setenv("TELCLAW_BASE_DELAY", "8")
    config.BASE_DELAY = 8

    before = next(row for row in backoffice_settings.rows()
                  if row["spec"].key == "TELCLAW_BASE_DELAY")
    assert before["source"] == ".env"
    assert before["effective"] == "8"

    backoffice_settings.save_override("TELCLAW_BASE_DELAY", "21", admin_id=123)
    assert config.BASE_DELAY == 21

    after = next(row for row in backoffice_settings.rows()
                 if row["spec"].key == "TELCLAW_BASE_DELAY")
    assert after["source"] == "Back Office"
    assert after["override"] == "21"
    assert after["effective"] == "21"


def test_reset_removes_override_and_restores_env_immediately(settings_db, monkeypatch):
    monkeypatch.setenv("TELCLAW_CRAWL_INTERVAL_MINUTES", "7.5")
    config.CRAWL_INTERVAL_MINUTES = 7.5

    backoffice_settings.save_override("TELCLAW_CRAWL_INTERVAL_MINUTES", "2.25")
    assert config.CRAWL_INTERVAL_MINUTES == 2.25

    backoffice_settings.reset_override("TELCLAW_CRAWL_INTERVAL_MINUTES")
    assert config.CRAWL_INTERVAL_MINUTES == 7.5
    assert "TELCLAW_CRAWL_INTERVAL_MINUTES" not in backoffice_settings.overrides()


def test_bootstrap_database_path_cannot_be_overridden(settings_db):
    with pytest.raises(ValueError, match="bootstrap setting"):
        backoffice_settings.save_override("TELCLAW_DB_NAME", "other.sqlite")


def test_provider_priority_is_rebuilt_from_effective_settings(settings_db, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER_1", "groq")
    monkeypatch.setenv("AI_PROVIDER_2", "cloudflare")
    config.AI_PROVIDERS = ("groq", "cloudflare")

    backoffice_settings.save_override("AI_PROVIDER_2", "")
    assert config.AI_PROVIDERS == ("groq",)

    backoffice_settings.reset_override("AI_PROVIDER_2")
    assert config.AI_PROVIDERS == ("groq", "cloudflare")


def test_secret_override_is_persisted_without_changing_env(settings_db, monkeypatch):
    monkeypatch.setenv("TELCLAW_TELEGRAM_BOT_TOKEN", "env-token")
    backoffice_settings.save_override("TELCLAW_TELEGRAM_BOT_TOKEN", "override-token")

    assert os.environ["TELCLAW_TELEGRAM_BOT_TOKEN"] == "env-token"
    assert config.TELEGRAM_BOT_TOKEN == "override-token"
    assert backoffice_settings.overrides()["TELCLAW_TELEGRAM_BOT_TOKEN"] == "override-token"


def test_invalid_numeric_setting_is_rejected(settings_db):
    with pytest.raises(ValueError, match="must be >= 1"):
        backoffice_settings.save_override("TELCLAW_BACKOFFICE_PORT", "0")

    with pytest.raises(ValueError, match="must be <= 3"):
        backoffice_settings.save_override("TELCLAW_ADVERTIO_CONCURRENCY", "4")
