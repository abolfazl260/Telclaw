import os

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int_env(name, default, *, minimum=None):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"Invalid value for {name}: must be >= {minimum}")
    return value


def _float_env(name, default, *, minimum=None):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid number for {name}: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"Invalid value for {name}: must be >= {minimum}")
    return value


API_ID = int(_required_env("TELEGRAM_API_ID"))
API_HASH = _required_env("TELEGRAM_API_HASH")
SESSION_DIR = os.getenv("TELCLAW_SESSION_DIR", "sessions")
DB_NAME = os.getenv("TELCLAW_DB_NAME", "telclaw.db")
CHANNELS_JSON = os.getenv("TELCLAW_CHANNELS_FILE", "channels.json")
ERROR_LOG_FILE = os.getenv("TELCLAW_ERROR_LOG", "crawler_errors.log")
MAX_MEDIA_SIZE = _int_env("TELCLAW_MAX_MEDIA_SIZE", 2 * 1024 * 1024, minimum=1)
MEDIA_DIR = os.getenv("TELCLAW_MEDIA_DIR", "media")
ADVERTIO_MEDIA_MAX_SIZE = _int_env("TELCLAW_ADVERTIO_MEDIA_MAX_SIZE", 8 * 1024 * 1024, minimum=1)
BASE_DELAY = _int_env("TELCLAW_BASE_DELAY", 8, minimum=0)
RANDOM_DELAY_MAX = _int_env("TELCLAW_RANDOM_DELAY_MAX", 400, minimum=0)
CRAWL_INTERVAL_MINUTES = _float_env("TELCLAW_CRAWL_INTERVAL_MINUTES", 5, minimum=0)
CHANNEL_INTERVAL_MINUTES = _float_env("TELCLAW_CHANNEL_INTERVAL_MINUTES", 1, minimum=0)
PROCESSING_INTERVAL_MINUTES = _float_env("TELCLAW_PROCESSING_INTERVAL_MINUTES", 1, minimum=0)
AI_INTERVAL_MINUTES = _float_env("TELCLAW_AI_INTERVAL_MINUTES", 1, minimum=0)
TELEGRAM_PROXY = os.getenv("TELECLAW_TELEGRAM_PROXY", "").strip()

TELEGRAM_MONITOR_ENABLED = os.getenv("TELCLAW_TELEGRAM_MONITOR_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
TELEGRAM_BOT_TOKEN = os.getenv("TELCLAW_TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_MONITOR_REPORT_INTERVAL_MINUTES = _float_env("TELCLAW_TELEGRAM_MONITOR_REPORT_INTERVAL_MINUTES", 30, minimum=0)

# AI providers are evaluated strictly in this order.
_SUPPORTED_AI_PROVIDERS = {"groq", "cloudflare"}
AI_PROVIDERS = tuple(
    value.strip().lower()
    for value in (os.getenv("AI_PROVIDER_1", "groq"), os.getenv("AI_PROVIDER_2", ""))
    if value.strip()
)
if not AI_PROVIDERS:
    raise RuntimeError("AI provider configuration is empty; set AI_PROVIDER_1")
if any(provider not in _SUPPORTED_AI_PROVIDERS for provider in AI_PROVIDERS):
    invalid = [provider for provider in AI_PROVIDERS if provider not in _SUPPORTED_AI_PROVIDERS]
    raise RuntimeError(f"Invalid AI provider(s): {', '.join(invalid)}. Supported providers: groq, cloudflare")
if len(AI_PROVIDERS) != len(set(AI_PROVIDERS)):
    raise RuntimeError("Duplicate AI providers are not allowed in AI_PROVIDER_1/AI_PROVIDER_2")

# Generic provider routing controls. Existing provider-specific settings remain supported.
AI_RETRY_COUNT = _int_env("AI_RETRY_COUNT", 3, minimum=0)
AI_TIMEOUT_SECONDS = _float_env("AI_TIMEOUT_SECONDS", 60, minimum=1)
AI_COOLDOWN_SECONDS = _float_env("AI_COOLDOWN_SECONDS", 200, minimum=0)
AI_RECOVERY_INTERVAL_SECONDS = _float_env("AI_RECOVERY_INTERVAL_SECONDS", 60, minimum=1)

# Existing Groq variables remain supported without migration.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("TELCLAW_GROQ_MODEL", "").strip()

CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CLOUDFLARE_MODEL = os.getenv("CLOUDFLARE_MODEL", "").strip()
CLOUDFLARE_REQUESTS_PER_MINUTE = _int_env("TELCLAW_CLOUDFLARE_REQUESTS_PER_MINUTE", 30, minimum=1)
CLOUDFLARE_TIMEOUT_SECONDS = _float_env("TELCLAW_CLOUDFLARE_TIMEOUT_SECONDS", AI_TIMEOUT_SECONDS, minimum=1)


def _build_groq_providers():
    providers = []
    for index in (1, 2, 3):
        suffix = "" if index == 1 else f"_{index}"
        key = os.getenv(f"GROQ_API_KEY{suffix}", "").strip()
        if not key:
            continue
        model = os.getenv(f"TELCLAW_GROQ_MODEL{suffix}", GROQ_MODEL).strip()
        if not model:
            raise RuntimeError(f"Missing required environment variable: TELCLAW_GROQ_MODEL{suffix}")
        providers.append({"api_key": key, "model": model})
    return providers


GROQ_PROVIDERS = _build_groq_providers()


def _build_cloudflare_providers():
    providers = []
    for index in (1, 2, 3):
        suffix = "" if index == 1 else f"_{index}"
        account_id = os.getenv(f"CLOUDFLARE_ACCOUNT_ID{suffix}", "").strip()
        api_token = os.getenv(f"CLOUDFLARE_API_TOKEN{suffix}", "").strip()
        model = os.getenv(f"CLOUDFLARE_MODEL{suffix}", CLOUDFLARE_MODEL).strip()
        if not any((account_id, api_token, model)):
            continue
        missing = [name for name, value in ((f"CLOUDFLARE_ACCOUNT_ID{suffix}", account_id), (f"CLOUDFLARE_API_TOKEN{suffix}", api_token), (f"CLOUDFLARE_MODEL{suffix}", model)) if not value]
        if missing:
            raise RuntimeError(f"Incomplete Cloudflare credential set: missing {', '.join(missing)}")
        providers.append({"account_id": account_id, "api_token": api_token, "model": model})
    return providers


CLOUDFLARE_PROVIDERS = _build_cloudflare_providers()
GROQ_FAILOVER_THRESHOLD_SECONDS = _float_env("TELCLAW_GROQ_FAILOVER_THRESHOLD_SECONDS", 200, minimum=0)
GROQ_REQUESTS_PER_MINUTE = _int_env("TELCLAW_GROQ_REQUESTS_PER_MINUTE", 30, minimum=1)
GROQ_RATE_LIMIT_MAX_RETRIES = _int_env("TELCLAW_GROQ_RATE_LIMIT_MAX_RETRIES", 5, minimum=0)
GROQ_RATE_LIMIT_MIN_WAIT_SECONDS = _float_env("TELCLAW_GROQ_RATE_LIMIT_MIN_WAIT_SECONDS", 30, minimum=1)
GROQ_RATE_LIMIT_MAX_WAIT_SECONDS = _float_env("TELCLAW_GROQ_RATE_LIMIT_MAX_WAIT_SECONDS", 180, minimum=GROQ_RATE_LIMIT_MIN_WAIT_SECONDS)
GROQ_MAX_COMPLETION_TOKENS = _int_env("TELCLAW_GROQ_MAX_COMPLETION_TOKENS", 1200, minimum=256)
GROQ_INVALID_JSON_MAX_RETRIES = _int_env("TELCLAW_GROQ_INVALID_JSON_MAX_RETRIES", 1, minimum=0)
AI_CLASSIFICATION_ENABLED = os.getenv("TELCLAW_AI_CLASSIFICATION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
AI_CLASSIFICATION_BATCH_SIZE = _int_env("TELCLAW_AI_CLASSIFICATION_BATCH_SIZE", 50, minimum=1)
AI_CLASSIFICATION_MAX_RETRIES = _int_env("TELCLAW_AI_CLASSIFICATION_MAX_RETRIES", 3, minimum=0)

AI_EXTRACTION_ENABLED = os.getenv("TELCLAW_AI_EXTRACTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
AI_EXTRACTION_CATEGORY_ENABLED = {
    "housinglist": os.getenv("TELCLAW_AI_EXTRACTION_HOUSINGLIST_ENABLED", str(AI_EXTRACTION_ENABLED)).lower() in {"1", "true", "yes", "on"},
    "transferlist": os.getenv("TELCLAW_AI_EXTRACTION_TRANSFERLIST_ENABLED", str(AI_EXTRACTION_ENABLED)).lower() in {"1", "true", "yes", "on"},
    "joblist": os.getenv("TELCLAW_AI_EXTRACTION_JOBLIST_ENABLED", str(AI_EXTRACTION_ENABLED)).lower() in {"1", "true", "yes", "on"},
}


def is_ai_extraction_enabled(category):
    return AI_EXTRACTION_ENABLED and AI_EXTRACTION_CATEGORY_ENABLED.get(category, False)


ADVERTIO_INGEST_ENABLED = os.getenv("TELCLAW_ADVERTIO_INGEST_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
ADVERTIO_BASE_URL = os.getenv("TELCLAW_ADVERTIO_BASE_URL", "https://api.advertio.ir").rstrip("/")
ADVERTIO_INGEST_KEY = os.getenv("TELCLAW_ADVERTIO_INGEST_KEY", "").strip()
ADVERTIO_SOURCE_NAME = os.getenv("TELCLAW_ADVERTIO_SOURCE_NAME", "telegram-rent").strip()
ADVERTIO_AUTO_PUBLISH = os.getenv("TELCLAW_ADVERTIO_AUTO_PUBLISH", "false").lower() in {"1", "true", "yes", "on"}
ADVERTIO_CONCURRENCY = max(1, min(3, _int_env("TELCLAW_ADVERTIO_CONCURRENCY", 3, minimum=1)))
ADVERTIO_TIMEOUT_SECONDS = _float_env("TELCLAW_ADVERTIO_TIMEOUT_SECONDS", 60, minimum=1)
