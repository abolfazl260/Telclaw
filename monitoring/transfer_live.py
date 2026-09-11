"""Telegram /transferlive command for active transfer advertisements."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from types import MethodType
from zoneinfo import ZoneInfo

from storage import database

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
MAX_MESSAGE_LENGTH = 3900
COUNTRY_NAMES = {
    "IR": "ایران", "DE": "آلمان", "TR": "ترکیه", "CA": "کانادا", "US": "آمریکا",
    "GB": "انگلستان", "FR": "فرانسه", "IT": "ایتالیا", "ES": "اسپانیا", "NL": "هلند",
    "BE": "بلژیک", "AT": "اتریش", "CH": "سوئیس", "SE": "سوئد", "NO": "نروژ",
    "DK": "دانمارک", "FI": "فنلاند", "PL": "لهستان", "GR": "یونان", "RU": "روسیه",
    "UA": "اوکراین", "AE": "امارات", "QA": "قطر", "SA": "عربستان", "KW": "کویت",
    "OM": "عمان", "IQ": "عراق", "AZ": "آذربایجان", "GE": "گرجستان", "AM": "ارمنستان",
    "CN": "چین", "JP": "ژاپن", "KR": "کره جنوبی", "IN": "هند", "PK": "پاکستان",
    "AF": "افغانستان",
}


def _display_date(value) -> str:
    """Return the stored Gregorian date unchanged for Telegram display."""
    text = str(value or "").strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return text[:10] or "-"


def _role_label(value) -> str:
    return {
        "passenger": "✈️ مسافر",
        "shipper": "📦 ارسال بار",
    }.get(str(value or "").strip().lower(), "نامشخص")


def _origin_label(row) -> str:
    country = str(row["origin_country"] or "").strip().upper()
    city = str(row["origin_city"] or "").strip()
    return COUNTRY_NAMES.get(country, country) or city or "نامشخص"


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
        return ["<b>🟢 آگهی های فعال</b>\n\n⚠️ در حال حاضر آگهی فعال حمل‌ونقل وجود ندارد."]

    chunks = []
    current = "<b>🟢 آگهی های فعال</b>\n\n"
    for origin, rows in groups.items():
        section = f"<b>📍 از مبدا {origin}</b>\n"
        for row in rows:
            username = str(row["sender_username"] or "").strip()
            username = username if username.startswith("@") else (f"@{username}" if username else "بدون یوزرنیم")
            section += f"{username} | {_display_date(row['departure_date'])} | {_role_label(row['transfer_role'])}\n"
        section += "\n"

        if len(current) + len(section) > MAX_MESSAGE_LENGTH and current.strip() != "<b>🟢 آگهی های فعال</b>":
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
            await self._send(chat_id, chunk, parse_mode="HTML")

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
