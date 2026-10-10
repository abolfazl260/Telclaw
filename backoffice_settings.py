"""Persistent Back Office overrides for .env-backed Telclaw configuration.

The process still boots from .env/config.py. Values stored here are applied after
the bootstrap database is available, so .env remains the fallback/default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from storage.database import get_connection


@dataclass(frozen=True)
class SettingSpec:
    key: str
    attr: str | None
    group: str
    kind: str = "str"
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False
    restart: bool = False
    readonly: bool = False
    fallback: str = ""


def _s(key, attr, group, **kwargs):
    return SettingSpec(key, attr, group, **kwargs)


SPECS = (
    _s("TELEGRAM_API_ID", "API_ID", "Telegram", kind="int", minimum=1, secret=True, restart=True),
    _s("TELEGRAM_API_HASH", "API_HASH", "Telegram", secret=True, restart=True),
    _s("TELCLAW_SESSION_DIR", "SESSION_DIR", "Runtime", restart=True, fallback="sessions"),
    _s("TELCLAW_DB_NAME", "DB_NAME", "Runtime", restart=True, readonly=True, fallback="telclaw.db"),
    _s("TELCLAW_CHANNELS_FILE", "CHANNELS_JSON", "Runtime", restart=True, fallback="channels.json"),
    _s("TELCLAW_ERROR_LOG", "ERROR_LOG_FILE", "Runtime", restart=True, fallback="crawler_errors.log"),
    _s("TELCLAW_MAX_MEDIA_SIZE", "MAX_MEDIA_SIZE", "Runtime", kind="int", minimum=1, fallback="2097152"),
    _s("TELCLAW_BASE_DELAY", "BASE_DELAY", "Runtime", kind="int", minimum=0, fallback="8"),
    _s("TELCLAW_RANDOM_DELAY_MAX", "RANDOM_DELAY_MAX", "Runtime", kind="int", minimum=0, fallback="400"),
    _s("TELCLAW_CRAWL_INTERVAL_MINUTES", "CRAWL_INTERVAL_MINUTES", "Runtime", kind="float", minimum=0, fallback="5"),
    _s("TELCLAW_CHANNEL_INTERVAL_MINUTES", "CHANNEL_INTERVAL_MINUTES", "Runtime", kind="float", minimum=0, fallback="1"),
    _s("TELCLAW_PROCESSING_INTERVAL_MINUTES", "PROCESSING_INTERVAL_MINUTES", "Runtime", kind="float", minimum=0, fallback="1"),
    _s("TELCLAW_AI_INTERVAL_MINUTES", "AI_INTERVAL_MINUTES", "Runtime", kind="float", minimum=0, fallback="1"),

    _s("AI_PROVIDER_1", None, "AI Providers", choices=("groq", "cloudflare"), restart=True, fallback="groq"),
    _s("AI_PROVIDER_2", None, "AI Providers", choices=("", "groq", "cloudflare"), restart=True),
    _s("AI_RETRY_COUNT", "AI_RETRY_COUNT", "AI Providers", kind="int", minimum=0, fallback="3"),
    _s("AI_TIMEOUT_SECONDS", "AI_TIMEOUT_SECONDS", "AI Providers", kind="float", minimum=1, fallback="60"),
    _s("AI_COOLDOWN_SECONDS", "AI_COOLDOWN_SECONDS", "AI Providers", kind="float", minimum=0, fallback="200"),
    _s("AI_RECOVERY_INTERVAL_SECONDS", "AI_RECOVERY_INTERVAL_SECONDS", "AI Providers", kind="float", minimum=1, fallback="60"),

    _s("GROQ_API_KEY", "GROQ_API_KEY", "Groq", secret=True, restart=True),
    _s("GROQ_API_KEY_2", None, "Groq", secret=True, restart=True),
    _s("GROQ_API_KEY_3", None, "Groq", secret=True, restart=True),
    _s("TELCLAW_GROQ_MODEL", "GROQ_MODEL", "Groq", restart=True),
    _s("TELCLAW_GROQ_MODEL_2", None, "Groq", restart=True),
    _s("TELCLAW_GROQ_MODEL_3", None, "Groq", restart=True),
    _s("TELCLAW_GROQ_REQUESTS_PER_MINUTE", "GROQ_REQUESTS_PER_MINUTE", "Groq", kind="int", minimum=1, fallback="30"),
    _s("TELCLAW_GROQ_FAILOVER_THRESHOLD_SECONDS", "GROQ_FAILOVER_THRESHOLD_SECONDS", "Groq", kind="float", minimum=0, fallback="200"),
    _s("TELCLAW_GROQ_RATE_LIMIT_MAX_RETRIES", "GROQ_RATE_LIMIT_MAX_RETRIES", "Groq", kind="int", minimum=0, fallback="5"),
    _s("TELCLAW_GROQ_RATE_LIMIT_MIN_WAIT_SECONDS", "GROQ_RATE_LIMIT_MIN_WAIT_SECONDS", "Groq", kind="float", minimum=1, fallback="30"),
    _s("TELCLAW_GROQ_RATE_LIMIT_MAX_WAIT_SECONDS", "GROQ_RATE_LIMIT_MAX_WAIT_SECONDS", "Groq", kind="float", minimum=1, fallback="180"),
    _s("TELCLAW_GROQ_MAX_COMPLETION_TOKENS", "GROQ_MAX_COMPLETION_TOKENS", "Groq", kind="int", minimum=256, fallback="1200"),
    _s("TELCLAW_GROQ_INVALID_JSON_MAX_RETRIES", "GROQ_INVALID_JSON_MAX_RETRIES", "Groq", kind="int", minimum=0, fallback="1"),

    _s("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID", "Cloudflare", secret=True, restart=True),
    _s("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN", "Cloudflare", secret=True, restart=True),
    _s("CLOUDFLARE_MODEL", "CLOUDFLARE_MODEL", "Cloudflare", restart=True),
    _s("CLOUDFLARE_ACCOUNT_ID_2", None, "Cloudflare", secret=True, restart=True),
    _s("CLOUDFLARE_API_TOKEN_2", None, "Cloudflare", secret=True, restart=True),
    _s("CLOUDFLARE_MODEL_2", None, "Cloudflare", restart=True),
    _s("CLOUDFLARE_ACCOUNT_ID_3", None, "Cloudflare", secret=True, restart=True),
    _s("CLOUDFLARE_API_TOKEN_3", None, "Cloudflare", secret=True, restart=True),
    _s("CLOUDFLARE_MODEL_3", None, "Cloudflare", restart=True),
    _s("TELCLAW_CLOUDFLARE_REQUESTS_PER_MINUTE", "CLOUDFLARE_REQUESTS_PER_MINUTE", "Cloudflare", kind="int", minimum=1, fallback="30"),
    _s("TELCLAW_CLOUDFLARE_TIMEOUT_SECONDS", "CLOUDFLARE_TIMEOUT_SECONDS", "Cloudflare", kind="float", minimum=1, fallback="60"),

    _s("TELCLAW_AI_CLASSIFICATION_ENABLED", "AI_CLASSIFICATION_ENABLED", "AI Classification", kind="bool", fallback="false"),
    _s("TELCLAW_AI_CLASSIFICATION_BATCH_SIZE", "AI_CLASSIFICATION_BATCH_SIZE", "AI Classification", kind="int", minimum=1, fallback="50"),
    _s("TELCLAW_AI_CLASSIFICATION_MAX_RETRIES", "AI_CLASSIFICATION_MAX_RETRIES", "AI Classification", kind="int", minimum=0, fallback="3"),
    _s("TELCLAW_AI_EXTRACTION_ENABLED", "AI_EXTRACTION_ENABLED", "AI Extraction", kind="bool", fallback="false"),
    _s("TELCLAW_AI_EXTRACTION_HOUSINGLIST_ENABLED", None, "AI Extraction", kind="bool", fallback="false"),
    _s("TELCLAW_AI_EXTRACTION_TRANSFERLIST_ENABLED", None, "AI Extraction", kind="bool", fallback="false"),
    _s("TELCLAW_AI_EXTRACTION_JOBLIST_ENABLED", None, "AI Extraction", kind="bool", fallback="false"),

    _s("TELCLAW_TELEGRAM_MONITOR_ENABLED", "TELEGRAM_MONITOR_ENABLED", "Monitoring", kind="bool", restart=True, fallback="true"),
    _s("TELCLAW_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "Monitoring", secret=True, restart=True),
    _s("TELCLAW_TELEGRAM_MONITOR_REPORT_INTERVAL_MINUTES", "TELEGRAM_MONITOR_REPORT_INTERVAL_MINUTES", "Monitoring", kind="float", minimum=0, fallback="30"),

    _s("TELCLAW_BACKOFFICE_ENABLED", "BACKOFFICE_ENABLED", "Back Office", kind="bool", restart=True, fallback="false"),
    _s("TELCLAW_BACKOFFICE_PUBLIC_URL", "BACKOFFICE_PUBLIC_URL", "Back Office", restart=True),
    _s("TELCLAW_BACKOFFICE_PUBLIC_PORT", "BACKOFFICE_PUBLIC_PORT", "Back Office", kind="int", minimum=0, restart=True, fallback="0"),
    _s("TELCLAW_BACKOFFICE_HOST", "BACKOFFICE_HOST", "Back Office", restart=True, fallback="127.0.0.1"),
    _s("TELCLAW_BACKOFFICE_PORT", "BACKOFFICE_PORT", "Back Office", kind="int", minimum=1, maximum=65535, restart=True, fallback="8787"),
    _s("TELCLAW_BACKOFFICE_TLS_CERT", "BACKOFFICE_TLS_CERT", "Back Office", restart=True),
    _s("TELCLAW_BACKOFFICE_TLS_KEY", "BACKOFFICE_TLS_KEY", "Back Office", secret=True, restart=True),

    _s("TELCLAW_ADVERTIO_INGEST_ENABLED", "ADVERTIO_INGEST_ENABLED", "Advertio", kind="bool", restart=True, fallback="false"),
    _s("TELCLAW_ADVERTIO_BASE_URL", "ADVERTIO_BASE_URL", "Advertio", restart=True, fallback="https://api.advertio.ir"),
    _s("TELCLAW_ADVERTIO_INGEST_KEY", "ADVERTIO_INGEST_KEY", "Advertio", secret=True, restart=True),
    _s("TELCLAW_ADVERTIO_SOURCE_NAME", "ADVERTIO_SOURCE_NAME", "Advertio", restart=True, fallback="telegram-rent"),
    _s("TELCLAW_ADVERTIO_AUTO_PUBLISH", "ADVERTIO_AUTO_PUBLISH", "Advertio", kind="bool", fallback="false"),
    _s("TELCLAW_ADVERTIO_CONCURRENCY", "ADVERTIO_CONCURRENCY", "Advertio", kind="int", minimum=1, maximum=3, restart=True, fallback="3"),
    _s("TELCLAW_ADVERTIO_TIMEOUT_SECONDS", "ADVERTIO_TIMEOUT_SECONDS", "Advertio", kind="float", minimum=1, fallback="60"),

    _s("TELCLAW_TRANSFER_TELEGRAM_PUBLISH_ENABLED", "TRANSFER_TELEGRAM_PUBLISH_ENABLED", "Transfer Publisher", kind="bool", restart=True, fallback="false"),
    _s("TELCLAW_TRANSFER_TELEGRAM_CHANNEL", "TRANSFER_TELEGRAM_CHANNEL", "Transfer Publisher", restart=True),
    _s("TELCLAW_TRANSFER_TELEGRAM_INTERVAL_MINUTES", "TRANSFER_TELEGRAM_INTERVAL_MINUTES", "Transfer Publisher", kind="float", minimum=0.1, fallback="1"),
    _s("TELCLAW_TRANSFER_TELEGRAM_TIMEOUT_SECONDS", "TRANSFER_TELEGRAM_TIMEOUT_SECONDS", "Transfer Publisher", kind="float", minimum=1, fallback="30"),
)

_BY_KEY = {item.key: item for item in SPECS}


def initialize():
    conn = get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS runtime_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by INTEGER
        )""")
        conn.commit()
    finally:
        conn.close()


def _parse(spec: SettingSpec, raw):
    text = str(raw if raw is not None else "").strip()
    if spec.choices and text not in spec.choices:
        raise ValueError(f"{spec.key} must be one of: {', '.join(repr(v) for v in spec.choices)}")
    if spec.kind == "bool":
        lowered = text.lower()
        if lowered not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"{spec.key} must be true or false")
        return lowered in {"1", "true", "yes", "on"}
    if spec.kind == "int":
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"{spec.key} must be an integer") from exc
    elif spec.kind == "float":
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"{spec.key} must be a number") from exc
    else:
        return text
    if spec.minimum is not None and value < spec.minimum:
        raise ValueError(f"{spec.key} must be >= {spec.minimum:g}")
    if spec.maximum is not None and value > spec.maximum:
        raise ValueError(f"{spec.key} must be <= {spec.maximum:g}")
    return value


def _serialize(spec, value):
    if spec.kind == "bool":
        return "true" if bool(value) else "false"
    return str(value)


def overrides():
    initialize()
    conn = get_connection()
    try:
        return {row["key"]: row["value"] for row in conn.execute(
            "SELECT key,value FROM runtime_settings"
        ).fetchall()}
    finally:
        conn.close()


def env_default(spec):
    if spec.key == "TELCLAW_CLOUDFLARE_TIMEOUT_SECONDS":
        return os.getenv(spec.key, os.getenv("AI_TIMEOUT_SECONDS", spec.fallback or "60"))
    if spec.key in {
        "TELCLAW_AI_EXTRACTION_HOUSINGLIST_ENABLED",
        "TELCLAW_AI_EXTRACTION_TRANSFERLIST_ENABLED",
        "TELCLAW_AI_EXTRACTION_JOBLIST_ENABLED",
    }:
        return os.getenv(spec.key, os.getenv("TELCLAW_AI_EXTRACTION_ENABLED", "false"))
    return os.getenv(spec.key, spec.fallback)


def effective_raw(spec, stored=None):
    values = stored if stored is not None else overrides()
    return values.get(spec.key, env_default(spec))


def save_override(key, raw_value, admin_id=None):
    spec = _BY_KEY.get(str(key))
    if spec is None:
        raise ValueError("Unknown setting")
    if spec.readonly:
        raise ValueError(f"{spec.key} is a bootstrap setting and can only be changed in .env")
    parsed = _parse(spec, raw_value)
    serialized = _serialize(spec, parsed)
    initialize()
    conn = get_connection()
    try:
        conn.execute("""INSERT INTO runtime_settings(key,value,updated_at,updated_by)
            VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            (spec.key, serialized, datetime.now(timezone.utc).isoformat(),
             int(admin_id) if admin_id is not None else None))
        conn.commit()
    finally:
        conn.close()
    apply_persisted_overrides()
    return serialized


def reset_override(key):
    spec = _BY_KEY.get(str(key))
    if spec is None:
        raise ValueError("Unknown setting")
    initialize()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM runtime_settings WHERE key=?", (spec.key,))
        conn.commit()
    finally:
        conn.close()
    apply_persisted_overrides()


def _raw(name, stored, fallback=""):
    return stored.get(name, os.getenv(name, fallback))


def _bool_raw(name, stored, fallback="false"):
    return str(_raw(name, stored, fallback)).strip().lower() in {"1", "true", "yes", "on"}


def apply_persisted_overrides():
    """Apply saved settings over config.py's .env-derived values.

    Existing service/client instances may cache values, so Back Office labels
    settings that require reconstruction/rebinding as restart-required.
    """
    stored = overrides()

    for spec in SPECS:
        if spec.readonly or spec.attr is None:
            continue
        setattr(config, spec.attr, _parse(spec, effective_raw(spec, stored)))

    providers = tuple(
        value.strip().lower()
        for value in (_raw("AI_PROVIDER_1", stored, "groq"), _raw("AI_PROVIDER_2", stored, ""))
        if value.strip()
    )
    if not providers or any(item not in {"groq", "cloudflare"} for item in providers):
        raise ValueError("AI provider priority contains an unsupported provider")
    if len(providers) != len(set(providers)):
        raise ValueError("AI provider priority cannot contain duplicates")
    config.AI_PROVIDERS = providers

    groq_model = str(_raw("TELCLAW_GROQ_MODEL", stored, getattr(config, "GROQ_MODEL", ""))).strip()
    config.GROQ_MODEL = groq_model
    config.GROQ_API_KEY = str(_raw("GROQ_API_KEY", stored, getattr(config, "GROQ_API_KEY", ""))).strip()
    groq = []
    for index in (1, 2, 3):
        suffix = "" if index == 1 else f"_{index}"
        key = str(_raw(f"GROQ_API_KEY{suffix}", stored, "")).strip()
        model = str(_raw(f"TELCLAW_GROQ_MODEL{suffix}", stored, groq_model)).strip()
        if key and model:
            groq.append({"api_key": key, "model": model})
    config.GROQ_PROVIDERS = groq

    cf_model = str(_raw("CLOUDFLARE_MODEL", stored, getattr(config, "CLOUDFLARE_MODEL", ""))).strip()
    config.CLOUDFLARE_MODEL = cf_model
    config.CLOUDFLARE_ACCOUNT_ID = str(_raw("CLOUDFLARE_ACCOUNT_ID", stored, getattr(config, "CLOUDFLARE_ACCOUNT_ID", ""))).strip()
    config.CLOUDFLARE_API_TOKEN = str(_raw("CLOUDFLARE_API_TOKEN", stored, getattr(config, "CLOUDFLARE_API_TOKEN", ""))).strip()
    cloudflare = []
    for index in (1, 2, 3):
        suffix = "" if index == 1 else f"_{index}"
        account = str(_raw(f"CLOUDFLARE_ACCOUNT_ID{suffix}", stored, "")).strip()
        token = str(_raw(f"CLOUDFLARE_API_TOKEN{suffix}", stored, "")).strip()
        model = str(_raw(f"CLOUDFLARE_MODEL{suffix}", stored, cf_model)).strip()
        if account and token and model:
            cloudflare.append({"account_id": account, "api_token": token, "model": model})
    config.CLOUDFLARE_PROVIDERS = cloudflare

    config.AI_EXTRACTION_CATEGORY_ENABLED = {
        "housinglist": _bool_raw("TELCLAW_AI_EXTRACTION_HOUSINGLIST_ENABLED", stored, str(config.AI_EXTRACTION_ENABLED)),
        "transferlist": _bool_raw("TELCLAW_AI_EXTRACTION_TRANSFERLIST_ENABLED", stored, str(config.AI_EXTRACTION_ENABLED)),
        "joblist": _bool_raw("TELCLAW_AI_EXTRACTION_JOBLIST_ENABLED", stored, str(config.AI_EXTRACTION_ENABLED)),
    }
    return len(stored)


def rows():
    stored = overrides()
    result = []
    for spec in SPECS:
        result.append({
            "spec": spec,
            "override": stored.get(spec.key),
            "default": env_default(spec),
            "effective": effective_raw(spec, stored),
            "source": "Back Office" if spec.key in stored else ".env",
        })
    return result
