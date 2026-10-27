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
    _s("TELCLAW_ADVERTIO_CARGO_ENABLED", "ADVERTIO_CARGO_ENABLED", "Advertio Cargo", kind="bool", fallback="true"),
    _s("TELCLAW_ADVERTIO_CARGO_AUTO_PUBLISH", "ADVERTIO_CARGO_AUTO_PUBLISH", "Advertio Cargo", kind="bool", fallback="true"),
    _s("TELCLAW_ADVERTIO_CARGO_BACKFILL_DAYS", "ADVERTIO_CARGO_BACKFILL_DAYS", "Advertio Cargo", kind="int", minimum=1, maximum=3650, fallback="30"),
    _s("TELCLAW_ADVERTIO_CARGO_BATCH_SIZE", "ADVERTIO_CARGO_BATCH_SIZE", "Advertio Cargo", kind="int", minimum=1, maximum=100, fallback="50"),
    _s("TELCLAW_ADVERTIO_CARGO_DAILY_LIMIT", "ADVERTIO_CARGO_DAILY_LIMIT", "Advertio Cargo", kind="int", minimum=1, maximum=500, fallback="250"),

    _s("TELCLAW_ADVERTIO_JOBS_ENABLED", "ADVERTIO_JOBS_ENABLED", "Advertio Jobs", kind="bool", fallback="false"),
    _s("TELCLAW_ADVERTIO_JOBS_AUTO_PUBLISH", "ADVERTIO_JOBS_AUTO_PUBLISH", "Advertio Jobs", kind="bool", fallback="true"),
    _s("TELCLAW_ADVERTIO_JOBS_BACKFILL_ENABLED", "ADVERTIO_JOBS_BACKFILL_ENABLED", "Advertio Jobs", kind="bool", fallback="false"),
    _s("TELCLAW_ADVERTIO_JOBS_BACKFILL_DAYS", "ADVERTIO_JOBS_BACKFILL_DAYS", "Advertio Jobs", kind="int", fallback="7", minimum=1, maximum=365),
    _s("TELCLAW_ADVERTIO_JOBS_FRESHNESS_DAYS", "ADVERTIO_JOBS_FRESHNESS_DAYS", "Advertio Jobs", kind="int", fallback="7", minimum=1, maximum=365),
    _s("TELCLAW_ADVERTIO_JOBS_BATCH_SIZE", "ADVERTIO_JOBS_BATCH_SIZE", "Advertio Jobs", kind="int", fallback="25", minimum=1, maximum=100),
    _s("TELCLAW_ADVERTIO_JOBS_DAILY_LIMIT", "ADVERTIO_JOBS_DAILY_LIMIT", "Advertio Jobs", kind="int", fallback="200", minimum=1, maximum=1000),
    _s("TELCLAW_ADVERTIO_JOBS_MAX_ATTEMPTS", "ADVERTIO_JOBS_MAX_ATTEMPTS", "Advertio Jobs", kind="int", fallback="8", minimum=1, maximum=100),
    _s("TELCLAW_ADVERTIO_JOBS_CLAIM_TIMEOUT_SECONDS", "ADVERTIO_JOBS_CLAIM_TIMEOUT_SECONDS", "Advertio Jobs", kind="int", fallback="1800", minimum=1, maximum=86400),
    _s("TELCLAW_ADVERTIO_DAILY_REQUEST_LIMIT", "ADVERTIO_DAILY_REQUEST_LIMIT", "Advertio", kind="int", minimum=1, maximum=10000, fallback="1000"),

    _s("TELCLAW_TRANSFER_TELEGRAM_PUBLISH_ENABLED", "TRANSFER_TELEGRAM_PUBLISH_ENABLED", "Transfer Publisher", kind="bool", restart=True, fallback="false"),
    _s("TELCLAW_TRANSFER_TELEGRAM_CHANNEL", "TRANSFER_TELEGRAM_CHANNEL", "Transfer Publisher", restart=True),
    _s("TELCLAW_TRANSFER_TELEGRAM_INTERVAL_MINUTES", "TRANSFER_TELEGRAM_INTERVAL_MINUTES", "Transfer Publisher", kind="float", minimum=0.1, fallback="1"),
    _s("TELCLAW_TRANSFER_TELEGRAM_TIMEOUT_SECONDS", "TRANSFER_TELEGRAM_TIMEOUT_SECONDS", "Transfer Publisher", kind="float", minimum=1, fallback="30"),
)


# Persian explanations shown by the Back Office settings hover help.
# Keep this mapping complete: every SPECS entry must have a non-empty explanation.
SETTING_HELP_FA = {
    "TELEGRAM_API_ID": "شناسه عددی برنامه تلگرام که برای اتصال حساب کاربری از طریق Telethon استفاده می‌شود؛ از my.telegram.org دریافت می‌شود.",
    "TELEGRAM_API_HASH": "کلید محرمانه برنامه تلگرام برای احراز هویت Telethon. آن را منتشر نکنید و فقط در محیط امن نگه دارید.",
    "TELCLAW_SESSION_DIR": "مسیر پوشه‌ای که فایل‌های نشست احراز هویت تلگرام در آن ذخیره می‌شوند.",
    "TELCLAW_DB_NAME": "نام یا مسیر فایل پایگاه داده Telclaw. این گزینه از این صفحه فقط خواندنی است و باید در تنظیمات راه‌اندازی تغییر کند.",
    "TELCLAW_CHANNELS_FILE": "مسیر فایل پیکربندی کانال‌ها و منابعی که کرالر باید بررسی کند.",
    "TELCLAW_ERROR_LOG": "مسیر فایل متنی ثبت خطاهای برنامه.",
    "TELCLAW_MAX_MEDIA_SIZE": "حداکثر اندازه فایل رسانه‌ای قابل پردازش، بر حسب بایت. فایل بزرگ‌تر از این حد ممکن است نادیده گرفته شود.",
    "TELCLAW_BASE_DELAY": "مکث پایه بین عملیات ارسال یا پردازش وابسته به زمان‌بندی، بر حسب ثانیه؛ افزایش آن سرعت را کمتر می‌کند.",
    "TELCLAW_RANDOM_DELAY_MAX": "حداکثر تأخیر تصادفی اضافه‌شونده به مکث پایه، بر حسب ثانیه؛ برای پراکنده‌کردن زمان عملیات است.",
    "TELCLAW_CRAWL_INTERVAL_MINUTES": "فاصله زمانی اجرای چرخه کرال منابع، بر حسب دقیقه.",
    "TELCLAW_CHANNEL_INTERVAL_MINUTES": "فاصله زمانی بین پردازش دو کانال در کرالر، بر حسب دقیقه.",
    "TELCLAW_PROCESSING_INTERVAL_MINUTES": "فاصله زمانی اجرای چرخه پردازش پیام‌های جمع‌آوری‌شده، بر حسب دقیقه.",
    "TELCLAW_AI_INTERVAL_MINUTES": "فاصله زمانی بررسی و اجرای کارهای صف هوش مصنوعی، بر حسب دقیقه.",
    "AI_PROVIDER_1": "اولین سرویس هوش مصنوعی که Telclaw برای درخواست‌ها امتحان می‌کند.",
    "AI_PROVIDER_2": "سرویس هوش مصنوعی جایگزین. اگر خالی باشد، سرویس دوم تعریف نشده است؛ در صورت تنظیم، ترتیب اولویت را رعایت کنید.",
    "AI_RETRY_COUNT": "حداکثر تعداد تلاش مجدد عمومی برای درخواست‌های هوش مصنوعی در مسیرهایی که از این تنظیم استفاده می‌کنند.",
    "AI_TIMEOUT_SECONDS": "حداکثر مدت انتظار برای پاسخ یک درخواست هوش مصنوعی، بر حسب ثانیه.",
    "AI_COOLDOWN_SECONDS": "مدت استراحت یک سرویس هوش مصنوعی پس از خطا یا موقتاً غیرقابل‌استفاده‌شدن، بر حسب ثانیه.",
    "AI_RECOVERY_INTERVAL_SECONDS": "فاصله بررسی مجدد سرویس‌های هوش مصنوعی که در وضعیت توقف موقت یا Cooldown هستند، بر حسب ثانیه.",
    "GROQ_API_KEY": "کلید API اصلی Groq. مقدار آن محرمانه است و در رابط کاربری نمایش داده نمی‌شود.",
    "GROQ_API_KEY_2": "کلید API دوم Groq برای استفاده از اعتبارنامه جایگزین یا توزیع درخواست‌ها.",
    "GROQ_API_KEY_3": "کلید API سوم Groq برای استفاده از اعتبارنامه جایگزین یا توزیع درخواست‌ها.",
    "TELCLAW_GROQ_MODEL": "نام مدل Groq که در پیکربندی اصلی استفاده می‌شود.",
    "TELCLAW_GROQ_MODEL_2": "نام مدل متصل به کلید دوم Groq، در صورت پیکربندی آن.",
    "TELCLAW_GROQ_MODEL_3": "نام مدل متصل به کلید سوم Groq، در صورت پیکربندی آن.",
    "TELCLAW_GROQ_REQUESTS_PER_MINUTE": "حداکثر تعداد درخواست‌های Groq در هر دقیقه که Telclaw برای محدودکردن نرخ استفاده می‌کند.",
    "TELCLAW_GROQ_FAILOVER_THRESHOLD_SECONDS": "مدتی که پس از آن، در صورت طولانی‌شدن وضعیت نامناسب Groq، امکان رفتن به سرویس جایگزین بررسی می‌شود.",
    "TELCLAW_GROQ_RATE_LIMIT_MAX_RETRIES": "حداکثر تعداد تلاش مجدد هنگام دریافت محدودیت نرخ یا پاسخ HTTP 429 از Groq.",
    "TELCLAW_GROQ_RATE_LIMIT_MIN_WAIT_SECONDS": "حداقل زمان انتظار بین تلاش‌های مجدد پس از محدودیت نرخ Groq، بر حسب ثانیه.",
    "TELCLAW_GROQ_RATE_LIMIT_MAX_WAIT_SECONDS": "حداکثر زمان انتظار برای تلاش مجدد پس از محدودیت نرخ Groq، بر حسب ثانیه.",
    "TELCLAW_GROQ_MAX_COMPLETION_TOKENS": "حداکثر تعداد توکن خروجی درخواستی از مدل Groq؛ مقدار بیشتر می‌تواند پاسخ طولانی‌تر و مصرف بیشتری ایجاد کند.",
    "TELCLAW_GROQ_INVALID_JSON_MAX_RETRIES": "تعداد تلاش مجدد اضافه برای بازیابی پاسخ JSON نامعتبر از Groq.",
    "CLOUDFLARE_ACCOUNT_ID": "شناسه حساب Cloudflare که API مدل هوش مصنوعی از آن استفاده می‌کند.",
    "CLOUDFLARE_API_TOKEN": "توکن محرمانه API برای دسترسی به مدل‌های Cloudflare. آن را با کسی به اشتراک نگذارید.",
    "CLOUDFLARE_MODEL": "شناسه مدل هوش مصنوعی Cloudflare که در پیکربندی اصلی استفاده می‌شود.",
    "CLOUDFLARE_ACCOUNT_ID_2": "شناسه حساب Cloudflare دوم برای مسیر جایگزین، در صورت استفاده.",
    "CLOUDFLARE_API_TOKEN_2": "توکن محرمانه حساب دوم Cloudflare برای مسیر جایگزین.",
    "CLOUDFLARE_MODEL_2": "شناسه مدل متصل به حساب دوم Cloudflare.",
    "CLOUDFLARE_ACCOUNT_ID_3": "شناسه حساب Cloudflare سوم برای مسیر جایگزین، در صورت استفاده.",
    "CLOUDFLARE_API_TOKEN_3": "توکن محرمانه حساب سوم Cloudflare برای مسیر جایگزین.",
    "CLOUDFLARE_MODEL_3": "شناسه مدل متصل به حساب سوم Cloudflare.",
    "TELCLAW_CLOUDFLARE_REQUESTS_PER_MINUTE": "حداکثر تعداد درخواست‌های Cloudflare در هر دقیقه که Telclaw مجاز به ارسال آن‌هاست.",
    "TELCLAW_CLOUDFLARE_TIMEOUT_SECONDS": "حداکثر زمان انتظار برای پاسخ Cloudflare، بر حسب ثانیه.",
    "TELCLAW_AI_CLASSIFICATION_ENABLED": "روشن یا خاموش‌کردن مرحله دسته‌بندی پیام‌ها با هوش مصنوعی. در حالت خاموش، پیام‌های جدید در این مرحله دسته‌بندی نمی‌شوند.",
    "TELCLAW_AI_CLASSIFICATION_BATCH_SIZE": "تعداد پیام‌هایی که در هر درخواست دسته‌بندی AI پردازش می‌شوند. مقدار بزرگ‌تر ممکن است سریع‌تر باشد اما خروجی بزرگ‌تری می‌سازد.",
    "TELCLAW_AI_CLASSIFICATION_MAX_RETRIES": "حداکثر تعداد تلاش مجدد برای دسته‌بندی پیام‌ها پس از خطا.",
    "TELCLAW_AI_EXTRACTION_ENABLED": "فعال یا غیرفعال‌کردن کلی استخراج اطلاعات ساختاریافته با هوش مصنوعی.",
    "TELCLAW_AI_EXTRACTION_HOUSINGLIST_ENABLED": "فعال یا غیرفعال‌کردن استخراج اختصاصی آگهی‌های مسکن. برای استخراج این دسته باید این گزینه و تنظیم کلی استخراج فعال باشند.",
    "TELCLAW_AI_EXTRACTION_TRANSFERLIST_ENABLED": "فعال یا غیرفعال‌کردن استخراج اختصاصی پیام‌های بار و ترنسفر. برای استخراج این دسته باید این گزینه و تنظیم کلی استخراج فعال باشند.",
    "TELCLAW_AI_EXTRACTION_JOBLIST_ENABLED": "فعال یا غیرفعال‌کردن استخراج اختصاصی آگهی‌های شغلی. این گزینه با فعال‌بودن ارسال Jobs به Advertio متفاوت است.",
    "TELCLAW_TELEGRAM_MONITOR_ENABLED": "فعال یا غیرفعال‌کردن سرویس مانیتورینگ تلگرام و گزارش‌های نظارتی.",
    "TELCLAW_TELEGRAM_BOT_TOKEN": "توکن ربات تلگرام مورد استفاده برای ارسال گزارش‌ها و اعلان‌های مانیتورینگ؛ محرمانه است.",
    "TELCLAW_TELEGRAM_MONITOR_REPORT_INTERVAL_MINUTES": "فاصله زمانی ارسال گزارش‌های دوره‌ای مانیتورینگ تلگرام، بر حسب دقیقه.",
    "TELCLAW_BACKOFFICE_ENABLED": "فعال یا غیرفعال‌کردن رابط مدیریتی وب Telclaw. تغییر این گزینه برای اعمال کامل به راه‌اندازی مجدد نیاز دارد.",
    "TELCLAW_BACKOFFICE_PUBLIC_URL": "آدرس عمومی پایه Back Office که برای ساخت لینک‌های ورود و دسترسی بیرونی استفاده می‌شود.",
    "TELCLAW_BACKOFFICE_PUBLIC_PORT": "پورتی که کاربران از بیرون برای دسترسی به Back Office استفاده می‌کنند؛ در صورت نیاز به پورت جدا از آدرس پایه تنظیم می‌شود.",
    "TELCLAW_BACKOFFICE_HOST": "آدرس شبکه‌ای که وب‌سرور Back Office روی آن گوش می‌دهد. تغییر آن نیازمند راه‌اندازی مجدد است.",
    "TELCLAW_BACKOFFICE_PORT": "پورت محلی وب‌سرور Back Office. باید با تنظیمات پروکسی و دسترسی عمومی هماهنگ باشد.",
    "TELCLAW_BACKOFFICE_TLS_CERT": "مسیر فایل گواهی TLS برای HTTPS مستقیم Back Office، در صورت استفاده.",
    "TELCLAW_BACKOFFICE_TLS_KEY": "مسیر فایل کلید خصوصی TLS برای HTTPS مستقیم Back Office؛ این فایل را محرمانه نگه دارید.",
    "TELCLAW_ADVERTIO_INGEST_ENABLED": "فعال یا غیرفعال‌کردن اتصال عمومی دریافت آگهی به API ادورتیو. این گزینه به‌تنهایی تنظیمات اختصاصی Cargo یا Jobs را جایگزین نمی‌کند.",
    "TELCLAW_ADVERTIO_BASE_URL": "آدرس پایه API ادورتیو که درخواست‌های ارسال آگهی به آن فرستاده می‌شوند.",
    "TELCLAW_ADVERTIO_INGEST_KEY": "کلید محرمانه احراز هویت Telclaw نزد API ادورتیو.",
    "TELCLAW_ADVERTIO_SOURCE_NAME": "نام منبعی که برای شناسایی مبدأ آگهی‌ها نزد ادورتیو ارسال می‌شود.",
    "TELCLAW_ADVERTIO_AUTO_PUBLISH": "تنظیم عمومی انتشار خودکار برای مسیرهای عمومی Ingest که از این گزینه استفاده می‌کنند؛ تنظیمات اختصاصی هر سرویس ممکن است جدا باشد.",
    "TELCLAW_ADVERTIO_CONCURRENCY": "حداکثر تعداد عملیات ارسال هم‌زمان به ادورتیو؛ افزایش آن می‌تواند بار درخواست‌ها را بیشتر کند.",
    "TELCLAW_ADVERTIO_TIMEOUT_SECONDS": "حداکثر زمان انتظار برای پاسخ درخواست‌های API ادورتیو، بر حسب ثانیه.",
    "TELCLAW_ADVERTIO_CARGO_ENABLED": "فعال یا غیرفعال‌کردن سرویس ارسال آگهی‌های Cargo/Transfer به ادورتیو.",
    "TELCLAW_ADVERTIO_CARGO_AUTO_PUBLISH": "اگر فعال باشد، آگهی‌های Cargo واجد شرایط با گزینه انتشار خودکار برای ادورتیو ارسال می‌شوند؛ پذیرش نهایی تابع API و بررسی‌های آن است.",
    "TELCLAW_ADVERTIO_CARGO_BACKFILL_DAYS": "بازه تاریخی بر حسب روز برای بررسی آگهی‌های قدیمی Cargo در عملیات Backfill؛ این مقدار به‌تنهایی به معنی ارسال خودکار همه موارد نیست.",
    "TELCLAW_ADVERTIO_CARGO_BATCH_SIZE": "حداکثر تعداد آگهی‌های Cargo که در هر نوبت پردازش دسته‌ای بررسی می‌شوند.",
    "TELCLAW_ADVERTIO_CARGO_DAILY_LIMIT": "سقف روزانه آگهی‌های Cargo که سرویس می‌تواند پردازش یا برای ارسال اقدام کند.",
    "TELCLAW_ADVERTIO_JOBS_ENABLED": "فعال یا غیرفعال‌کردن سرویس یکپارچه‌سازی آگهی‌های شغلی با Advertio.",
    "TELCLAW_ADVERTIO_JOBS_AUTO_PUBLISH": "برای آگهی‌های Jobs واجد شرایط، درخواست انتشار خودکار به ادورتیو می‌فرستد. این گزینه تضمین نمی‌کند که ادورتیو آگهی را فوراً Active کند.",
    "TELCLAW_ADVERTIO_JOBS_BACKFILL_ENABLED": "اجازه اجرای Backfill کنترل‌شده برای آگهی‌های شغلی قدیمی را می‌دهد؛ فعال‌کردن آن به‌تنهایی عملیات ارسال Backfill را شروع نمی‌کند.",
    "TELCLAW_ADVERTIO_JOBS_BACKFILL_DAYS": "حداکثر بازه تاریخی Backfill آگهی‌های شغلی، بر حسب روز. محدودیت تازگی نیز ممکن است بازه مؤثر را کوچک‌تر کند.",
    "TELCLAW_ADVERTIO_JOBS_FRESHNESS_DAYS": "حداکثر سن مجاز آگهی شغلی بر اساس تاریخ اصلی پیام تلگرام، بر حسب روز؛ آگهی قدیمی‌تر ممکن است از ارسال عادی کنار گذاشته شود.",
    "TELCLAW_ADVERTIO_JOBS_BATCH_SIZE": "حداکثر تعداد رکورد Jobs که در هر نوبت ارسال یا بررسی صف پردازش می‌شوند.",
    "TELCLAW_ADVERTIO_JOBS_DAILY_LIMIT": "سقف روزانه پردازش/ارسال آگهی‌های Jobs. محدودیت بودجه مشترک API نیز ممکن است زودتر مانع درخواست شود.",
    "TELCLAW_ADVERTIO_JOBS_MAX_ATTEMPTS": "حداکثر تعداد تلاش‌های تحویل هر آگهی Jobs پیش از توقف تلاش مجدد.",
    "TELCLAW_ADVERTIO_JOBS_CLAIM_TIMEOUT_SECONDS": "مدت اعتبار قفل یک رکورد در حال ارسال، بر حسب ثانیه؛ پس از این زمان امکان بازیابی رکورد گیرکرده بررسی می‌شود.",
    "TELCLAW_ADVERTIO_DAILY_REQUEST_LIMIT": "سقف تعداد درخواست‌های API ادورتیو در روز برای مسیرهایی که از بودجه مشترک درخواست‌ها استفاده می‌کنند.",
    "TELCLAW_TRANSFER_TELEGRAM_PUBLISH_ENABLED": "فعال یا غیرفعال‌کردن انتشار آگهی‌های Transfer در کانال تلگرامی مقصد.",
    "TELCLAW_TRANSFER_TELEGRAM_CHANNEL": "شناسه یا نام کاربری کانال تلگرامی مقصد برای انتشار آگهی‌های Transfer.",
    "TELCLAW_TRANSFER_TELEGRAM_INTERVAL_MINUTES": "فاصله زمانی بین چرخه‌های انتشار آگهی Transfer در تلگرام، بر حسب دقیقه.",
    "TELCLAW_TRANSFER_TELEGRAM_TIMEOUT_SECONDS": "حداکثر زمان انتظار برای عملیات انتشار Transfer در تلگرام، بر حسب ثانیه.",
}

def setting_help_fa(key: str) -> str:
    """Return the Persian hover-help text for a Back Office setting."""
    return SETTING_HELP_FA.get(key, "")

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
