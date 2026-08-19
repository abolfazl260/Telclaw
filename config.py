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
BASE_DELAY = int(os.getenv("TELCLAW_BASE_DELAY", "8"))
RANDOM_DELAY_MAX = int(os.getenv("TELCLAW_RANDOM_DELAY_MAX", "400"))

# Scheduled crawler settings. Default: every 5 minutes.
# Override with TELCLAW_CRAWL_INTERVAL_MINUTES=15, for example.
CRAWL_INTERVAL_MINUTES = float(
    os.getenv("TELCLAW_CRAWL_INTERVAL_MINUTES", "5")
)

# Optional Telegram SOCKS5 proxy. Leave empty for a direct connection.
TELEGRAM_PROXY = os.getenv("TELECLAW_TELEGRAM_PROXY", "").strip()

# AI extraction settings. Groq provides an OpenAI-compatible API.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("TELCLAW_GROQ_MODEL", "llama-3.1-8b-instant").strip()
AI_EXTRACTION_ENABLED = os.getenv("TELCLAW_AI_EXTRACTION_ENABLED", "false").lower() in {
    "1", "true", "yes", "on"
}
