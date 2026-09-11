"""Telegram /transferlive command for active transfer advertisements."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from types import MethodType
from zoneinfo import ZoneInfo

from storage import database

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
MAX_MESSAGE_LENGTH = 3900


def _jalali(gregorian_date: date) -> str:
    gy, gm, gd = gregorian_date.year, gregorian_date.month, gregorian_date.day
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def _display_date(value) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d").date()
        return _jalali(parsed)
    except ValueError:
        return text[:10] or "-"


def _role_label(value) -> str:
    return {
        "passenger": "✈️ مسافر",
        "shipper": "📦 ارسال بار",
    }.get(str(value or "").strip().lower(), "نامشخص")


def _origin_label(row) -> str:
    country = str(row["origin_country"] or "").strip()
    city = str(row["origin_city"] or "").strip()
    return country or city or "نامشخص"


def _fetch_active_rows():
    today = datetime.now(TEHRAN_TZ).date().isoformat()
    conn = database.get_connection()
    try:
        return conn.execute(
            """SELECT t.origin_country, t.origin_city, t.destination_city,
                      t.departure_date, t.transfer_role, m.sender_username
               FROM transferlist t
               JOIN messages m ON m.id = t.processed_message_id
               WHERE m.ai_status = 'processed'
                 AND m.ai_category = 'transferlist'
                 AND t.departure_date IS NOT NULL
                 AND date(substr(t.departure_date, 1, 10)) >= date(?)
               ORDER BY COALESCE(t.origin_country, t.origin_city, ''),
                        date(substr(t.departure_date, 1, 10)) ASC,
                        m.id ASC""",
            (today,),
        ).fetchall()
    finally:
        conn.close()


def _build_messages():
    groups = OrderedDict()
    for row in _fetch_active_rows():
        origin = _origin_label(row)
        groups.setdefault(origin, []).append(row)

    if not groups:
        return ["⚠️ در حال حاضر آگهی فعال حمل‌ونقل وجود ندارد."]

    chunks = []
    current = "🚚 آگهی‌های فعال حمل‌ونقل\n\n"
    for origin, rows in groups.items():
        section = f"📍 از مبدا {origin}\n"
        for row in rows:
            username = str(row["sender_username"] or "").strip()
            username = username if username.startswith("@") else (f"@{username}" if username else "بدون یوزرنیم")
            section += f"{username} | {_display_date(row['departure_date'])} | {_role_label(row['transfer_role'])}\n"
        section += "\n"

        if len(current) + len(section) > MAX_MESSAGE_LENGTH and current.strip() != "🚚 آگهی‌های فعال حمل‌ونقل":
            chunks.append(current.rstrip())
            current = section
        else:
            current += section

    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def install_transfer_live_command(monitor):
    """Install /transferlive without creating a second Telegram polling loop."""
    original_handle_update = monitor._handle_update
    original_register_commands = monitor._register_commands

    async def handle_update(self, update):
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip().lower()
        command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
        if command != "/transferlive":
            return await original_handle_update(update)

        if chat_id is None:
            return
        if not self._is_subscribed(chat_id):
            await self._send(chat_id, "⛔ ابتدا با /start دریافت گزارش‌های Telclaw را فعال کنید.")
            return

        for chunk in _build_messages():
            await self._send(chat_id, chunk)

    async def register_commands(self):
        await original_register_commands()
        commands = [
            {"command": "start", "description": "فعال‌سازی دریافت گزارش‌ها"},
            {"command": "stop", "description": "توقف دریافت گزارش‌ها"},
            {"command": "status", "description": "نمایش وضعیت فعلی سیستم"},
            {"command": "health", "description": "بررسی سلامت فعلی سیستم"},
            {"command": "today", "description": "نمایش آمار امروز"},
            {"command": "source", "description": "نمایش کانال‌ها و گروه‌های تحت کرال"},
            {"command": "down_errors", "description": "Download crawler error log"},
            {"command": "database", "description": "Download full SQLite database"},
            {"command": "transferlive", "description": "نمایش آگهی‌های فعال حمل‌ونقل"},
        ]
        try:
            await self._api("setMyCommands", {"commands": commands})
        except Exception:
            pass

    monitor._handle_update = MethodType(handle_update, monitor)
    monitor._register_commands = MethodType(register_commands, monitor)
    return monitor
