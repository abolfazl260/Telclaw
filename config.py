import os
API_ID = 24511039          # به صورت عدد وارد شود (بدون کوتیشن)
API_HASH = '822734bf2665f3895467eb9104957699'  # به صورت رشته داخل کوتیشن وارد شود
# تنظیمات دانلود و تاخیرها
MAX_MEDIA_SIZE = 2 * 1024 * 1024  # حداکثر ۲ مگابایت برای دانلود عکس‌ها
BASE_DELAY = 8                    # تاخیر پایه پیش‌فرض بین دریافت هر پیام (به ثانیه)
RANDOM_DELAY_MAX = 400            # حداکثر تاخیر تصادفی (۲ دقیقه) در زمان شبیه‌سازی رفتار انسانی

SESSION_DIR = 'sessions/'
ERROR_LOG_FILE = 'crawler_errors.log'
CHANNELS_JSON = 'channels.json'
