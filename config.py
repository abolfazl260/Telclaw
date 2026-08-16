import os


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Set it before starting Telclaw."
        )
    return value


# Telegram credentials must never be committed to source control.
API_ID = int(_required_env("TELEGRAM_API_ID"))
API_HASH = _required_env("TELEGRAM_API_HASH")

# Runtime storage/configuration
SESSION_DIR = os.getenv("TELCLAW_SESSION_DIR", "sessions")
DB_NAME = os.getenv("TELCLAW_DB_NAME", "telclaw.db")
CHANNELS_JSON = os.getenv("TELCLAW_CHANNELS_FILE", "channels.json")
ERROR_LOG_FILE = os.getenv("TELCLAW_ERROR_LOG", "crawler_errors.log")

# Crawler settings
MAX_MEDIA_SIZE = int(os.getenv("TELCLAW_MAX_MEDIA_SIZE", str(2 * 1024 * 1024)))
BASE_DELAY = int(os.getenv("TELCLAW_BASE_DELAY", "8"))
RANDOM_DELAY_MAX = int(os.getenv("TELCLAW_RANDOM_DELAY_MAX", "400"))
