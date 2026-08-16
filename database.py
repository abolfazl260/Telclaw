import sqlite3
from datetime import datetime
import config

def get_connection():
    """برقراری اتصال به دیتابیس SQLite"""
    conn = sqlite3.connect(config.DB_NAME)
    # تغییر رفتار برای بازگرداندن نتایج به صورت دیکشنری (خوانایی بهتر)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_db():
    """ایجاد جداول دیتابیس در صورت عدم وجود"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # ۱. جدول ذخیره پیام‌ها
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            text TEXT,
            date TEXT NOT NULL,
            media_path TEXT,
            UNIQUE(channel_username, message_id) -- جلوگیری از ثبت پیام تکراری
        )
    ''')
    
    # ۲. جدول تنظیمات کرالر (تاریخ انتخابی کاربر برای هر کانال)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crawler_settings (
            channel_username TEXT PRIMARY KEY,
            target_date TEXT NOT NULL,
            last_crawled_date TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

def insert_message(channel_username, message_id, text, date_str, media_path=None):
    """درج پیام جدید با مدیریت عدم همپوشانی و تکرار"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT OR IGNORE INTO messages (channel_username, message_id, text, date, media_path)
            VALUES (?, ?, ?, ?, ?)
        ''', (channel_username, message_id, text, date_str, media_path))
        conn.commit()
        # اگر سطر جدیدی اضافه شده باشد، True برمی‌گرداند
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        # خطاهای احتمالی دیتابیس این‌جا مدیریت یا مجدد ارسال می‌شوند
        raise e
    finally:
        conn.close()

def set_channel_target_date(channel_username, target_date_str):
    """تنظیم یا بروزرسانی تاریخ هدف کاربر برای یک کانال مشخص"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO crawler_settings (channel_username, target_date)
        VALUES (?, ?)
        ON CONFLICT(channel_username) DO UPDATE SET target_date = excluded.target_date
    ''', (channel_username, target_date_str))
    conn.commit()
    conn.close()

def get_channel_target_date(channel_username):
    """دریافت تاریخ هدف تعیین شده برای یک کانال"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT target_date FROM crawler_settings WHERE channel_username = ?', (channel_username,))
    row = cursor.fetchone()
    conn.close()
    return row['target_date'] if row else None

# اجرای اولیه برای ساخت دیتابیس در صورت اجرا شدن مستقیم فایل
if __name__ == '__main__':
    initialize_db()
    print(f"Database {config.DB_NAME} initialized successfully.")