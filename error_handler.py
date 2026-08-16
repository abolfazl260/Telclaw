import asyncio
import logging
import sys
from datetime import datetime
import config
from telethon.errors import FloodWaitError

# تنظیمات پایه برای کتابخانه logging پایتون جهت ذخیره خطاهای ماژول‌ها در فایل مجزا
logging.basicConfig(
    filename=config.ERROR_LOG_FILE,
    filemode='a',
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    level=logging.ERROR,
    encoding='utf-8'
)

def log_exception(e, custom_message="An error occurred"):
    """ثبت خطا در فایل log بدون نمایش در ترمینال اصلی"""
    logging.error(f"{custom_message}: {str(e)}", exc_info=True)

async def handle_telegram_errors(func, *args, **kwargs):
    """
    یک Wrapper ناهمگام برای مدیریت هوشمند خطای FloodWait و قطعی شبکه.
    این تابع متد مَد نظر را اجرا کرده و در صورت بروز خطا، مکانیزم تکرار را فعال می‌کند.
    """
    while True:
        try:
            # تلاش برای اجرای تابع اصلی تلگرام (مثلاً دریافت پیام‌ها)
            return await func(*args, **kwargs)
            
        except FloodWaitError as e:
            # استخراج ثانیه‌های اعلام شده توسط تلگرام و به خواب بردن برنامه
            error_msg = f"FloodWaitError: Sleeping for {e.seconds} seconds required by Telegram."
            log_exception(e, error_msg)
            
            # ارسال سیگنال به سیستم UI جهت نمایش در دیتابورد ترمینال (اختیاری)
            print(f"\n[⚠️ FLOOD WAIT] {error_msg}") 
            
            await asyncio.sleep(e.seconds)
            print("[🔄 INFO] Resuming crawler after FloodWait...")
            
        except (ConnectionError, IOError) as e:
            # مدیریت قطعی اینترنت یا پروکسی با مکانیزم تلاش مجدد ۶۰ ثانیه‌ای
            error_msg = "Network or Proxy connection lost. Retrying in 60 seconds..."
            log_exception(e, error_msg)
            
            print(f"\n[❌ NETWORK ERROR] {error_msg}")
            await asyncio.sleep(60)
            
        except Exception as e:
            # ثبت سایر خطاهای غیرمنتظره و متوقف نکردن کل اپلیکیشن
            log_exception(e, "Critical unexpected error inside crawler loop")
            print(f"\n[🔥 CRITICAL ERROR] Check {config.ERROR_LOG_FILE} for details.")
            break # یا بر حسب نیاز return None