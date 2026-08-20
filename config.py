import os

# Load local .env when python-dotenv is installed. Environment variables always
# take precedence, so production deployments can continue to inject secrets
# directly through the process environment.
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in .env or in the process environment before starting Telclaw."
        )
    return value


API_ID = int(_required_env("TELEGRAM_API_ID"))
API_HASH = _required_env("TELEGRAM_API_HASH")

SESSION_DIR = os.getenv("TELCLAW_SESSION_DIR", "sessions")
DB_NAME = os.getenv("TELCLAW_DB_NAME", "telclaw.db")
CHANNELS_JSON = os.getenv("TELCLAW_CHANNELS_FILE", "channels.json")
ERROR_LOG_FILE = os.getenv("TELCLAW_ERROR_LOG", "crawler_errors.log")

MAX_MEDIA_SIZE = int(os.getenv("TELCLAW_MAX_MEDIA_SIZE", str(2 * 1024 * 1024)))
MEDIA_DIR = os.getenv("TELCLAW_MEDIA_DIR", "media")
ADVERTIO_MEDIA_MAX_SIZE = int(os.getenv("TELCLAW_ADVERTIO_MEDIA_MAX_SIZE", str(8 * 1024 * 1024)))
BASE_DELAY = int(os.getenv("TELCLAW_BASE_DELAY", "8"))
RANDOM_DELAY_MAX = int(os.getenv("TELCLAW_RANDOM_DELAY_MAX", "400"))

CRAWL_INTERVAL_MINUTES = float(os.getenv("TELCLAW_CRAWL_INTERVAL_MINUTES", "5"))
PROCESSING_INTERVAL_MINUTES = float(os.getenv("TELCLAW_PROCESSING_INTERVAL_MINUTES", "1"))
AI_INTERVAL_MINUTES = float(os.getenv("TELCLAW_AI_INTERVAL_MINUTES", "1"))

TELEGRAM_PROXY = os.getenv("TELECLAW_TELEGRAM_PROXY", "").strip()

GROQ_API_KEY = _required_env("GROQ_API_KEY").strip()
GROQ_MODEL = _required_env("TELCLAW_GROQ_MODEL").strip()
if not GROQ_MODEL:
    raise RuntimeError("TELCLAW_GROQ_MODEL cannot be empty. Set it to an active Groq model.")

GROQ_REQUESTS_PER_MINUTE = int(os.getenv("TELCLAW_GROQ_REQUESTS_PER_MINUTE", "30"))
GROQ_RATE_LIMIT_MAX_RETRIES = max(0, int(os.getenv("TELCLAW_GROQ_RATE_LIMIT_MAX_RETRIES", "5")))
GROQ_RATE_LIMIT_MIN_WAIT_SECONDS = max(1, float(os.getenv("TELCLAW_GROQ_RATE_LIMIT_MIN_WAIT_SECONDS", "30")))
GROQ_RATE_LIMIT_MAX_WAIT_SECONDS = max(
    GROQ_RATE_LIMIT_MIN_WAIT_SECONDS,
    float(os.getenv("TELCLAW_GROQ_RATE_LIMIT_MAX_WAIT_SECONDS", "180")),
)
# Keep JSON responses compact. This is configurable because Groq model limits
# differ, but the default is enough for the deliberately minimal extraction shape.
GROQ_MAX_COMPLETION_TOKENS = max(256, int(os.getenv("TELCLAW_GROQ_MAX_COMPLETION_TOKENS", "1200")))
GROQ_INVALID_JSON_MAX_RETRIES = max(0, int(os.getenv("TELCLAW_GROQ_INVALID_JSON_MAX_RETRIES", "1")))
AI_EXTRACTION_ENABLED = os.getenv("TELCLAW_AI_EXTRACTION_ENABLED", "false").lower() in {
    "1", "true", "yes", "on"
}

ADVERTIO_INGEST_ENABLED = os.getenv("TELCLAW_ADVERTIO_INGEST_ENABLED", "false").lower() in {
    "1", "true", "yes", "on"
}
ADVERTIO_BASE_URL = os.getenv("TELCLAW_ADVERTIO_BASE_URL", "https://api.advertio.ir").rstrip("/")
ADVERTIO_INGEST_KEY = os.getenv("TELCLAW_ADVERTIO_INGEST_KEY", "").strip()
ADVERTIO_SOURCE_NAME = os.getenv("TELCLAW_ADVERTIO_SOURCE_NAME", "telegram-rent").strip()
ADVERTIO_AUTO_PUBLISH = os.getenv("TELCLAW_ADVERTIO_AUTO_PUBLISH", "false").lower() in {
    "1", "true", "yes", "on"
}
ADVERTIO_CONCURRENCY = max(1, min(3, int(os.getenv("TELCLAW_ADVERTIO_CONCURRENCY", "3"))))
ADVERTIO_TIMEOUT_SECONDS = float(os.getenv("TELCLAW_ADVERTIO_TIMEOUT_SECONDS", "60"))
