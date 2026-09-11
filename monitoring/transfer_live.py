"""Telegram /transferlive command using Telegram Bot API Rich Messages."""
from __future__ import annotations

import html
from collections import OrderedDict
from datetime import date, datetime, timedelta
from types import MethodType
from zoneinfo import ZoneInfo

from storage import database

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
MAX_RICH_MESSAGE_LENGTH = 30000
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


def _jalali(gregorian_date: date) -> str:
    gy, gm, gd = gregorian_date.year, gregorian_date.month, gregorian_date.day
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + ((gy2 + 3) // 4) - ((gy2 + 99) // 100)
            + ((gy2 + 399) // 400) + gd + gdm[gm - 1])
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


def _dual_date(value) -> tuple[str, str, date | None]:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d").date()
        return parsed.strftime("%Y-%m-%d"), _jalali(parsed), parsed
    except ValueError:
        return text[:10] or "-", "-", None


def _role_label(value) -> str:
    return {
        "passenger": "✈️ مسافر",
        "shipper": "📦 ارسال بار",
    }.get(str(value or "").strip().lower(), "❔ نامشخص")


def _role_icon(value) -> str:
    return "✈️" if str(value or "").strip().lower() == "passenger" else "📦"


def _origin_label(row) -> str:
    country = str(row["origin_country"] or "").strip().upper()
    city = str(row["origin_city"] or "").strip()
    return COUNTRY_NAMES.get(country, country) or city or "نامشخص"


def _destination_label(row) -> str:
    return str(row["destination_city"] or "").strip() or "نامشخص"


def _remaining_label(departure: date | None, today: date) -> str:
    if departure is None:
        return "—"
    delta = (departure - today).days
    if delta < 0:
        return "منقضی"
    if delta == 0:
        return "🔥 امروز"
    if delta == 1:
        return "⏳ فردا"
    if delta <= 7:
        return f"⏳ {delta} روز"
    return f"📅 {delta} روز"


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


def _cell(text: str, *, header: bool = False, align: str = "right") -> str:
    tag = "th" if header else "td"
    return f"<{tag} align=\"{align}\">{html.escape(str(text))}</{tag}>"


def _origin_table(origin: str, rows, today: date) -> str:
    table_rows = [
        "<tr>"
        + _cell("کاربر", header=True)
        + _cell("مقصد", header=True)
        + _cell("میلادی", header=True, align="center")
        + _cell("شمسی", header=True, align="center")
        + _cell("مانده", header=True, align="center")
        + _cell("نوع", header=True)
        + "</tr>"
    ]

    for row in rows:
        username = str(row["sender_username"] or "").strip()
        username = username if username.startswith("@") else (f"@{username}" if username else "بدون یوزرنیم")
        gregorian, jalali, departure = _dual_date(row["departure_date"])
        table_rows.append(
            "<tr>"
            + _cell(username)
            + _cell(_destination_label(row))
            + _cell(gregorian, align="center")
            + _cell(jalali, align="center")
            + _cell(_remaining_label(departure, today), align="center")
            + _cell(f"{_role_icon(row['transfer_role'])} {_role_label(row['transfer_role']).replace('✈️ ', '').replace('📦 ', '').replace('❔ ', '')}")
            + "</tr>"
        )

    return (
        f"<h3>📍 از مبدا {html.escape(origin)}</h3>"
        f"<table bordered striped compact>"
        f"<caption>{len(rows)} آگهی فعال</caption>"
        f"{''.join(table_rows)}"
        f"</table>"
    )


def _build_messages():
    rows = _fetch_active_rows()
    today = datetime.now(TEHRAN_TZ).date()

    groups = OrderedDict()
    for row in rows:
        groups.setdefault(_origin_label(row), []).append(row)

    if not groups:
        return [
            {
                "html": (
                    "<h1>🟢 آگهی های فعال</h1>"
                    "<p>⚠️ در حال حاضر آگهی فعال حمل‌ونقل وجود ندارد.</p>"
                    "<footer>Telclaw • به‌روزرسانی خودکار از SQLite</footer>"
                )
            }
        ]

    total = len(rows)
    passenger_count = sum(str(r["transfer_role"] or "").lower() == "passenger" for r in rows)
    shipper_count = sum(str(r["transfer_role"] or "").lower() == "shipper" for r in rows)
    unknown_count = total - passenger_count - shipper_count
    generated = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M")

    prefix = (
        "<h1>🟢 آگهی های فعال</h1>"
        f"<p><b>📊 خلاصه وضعیت</b> — <b>{total}</b> آگهی در <b>{len(groups)}</b> مبدا</p>"
        "<table bordered compact>"
        "<tr><th>نوع</th><th>تعداد</th></tr>"
        f"<tr><td>✈️ مسافر</td><td align=\"center\">{passenger_count}</td></tr>"
        f"<tr><td>📦 ارسال بار</td><td align=\"center\">{shipper_count}</td></tr>"
        f"<tr><td>❔ نامشخص</td><td align=\"center\">{unknown_count}</td></tr>"
        "</table>"
        "<hr/>"
    )
    suffix = (
        "<hr/>"
        f"<p>🕐 <b>آخرین بروزرسانی:</b> {generated} تهران</p>"
        "<p><i>تاریخ فعال بودن آگهی با تاریخ میلادی دیتابیس محاسبه می‌شود.</i></p>"
        "<tg-button-row align=\"center\">"
        "<tg-button type=\"callback_data\" style=\"success\" data=\"transferlive:refresh\">🔄 بروزرسانی</tg-button>"
        "</tg-button-row>"
        "<footer>Telclaw • Active Transfer Monitor</footer>"
    )

    chunks = []
    current = prefix
    for origin, origin_rows in groups.items():
        section = _origin_table(origin, origin_rows, today)
        if len(current) + len(section) + len(suffix) > MAX_RICH_MESSAGE_LENGTH and current != prefix:
            chunks.append({"html": current + suffix})
            current = prefix
        current += section
    chunks.append({"html": current + suffix})
    return chunks


async def _send_rich(monitor, chat_id: int, rich_message: dict) -> None:
    """Send a native Telegram Rich Message through Bot API 10.3."""
    await monitor._api(
        "sendRichMessage",
        {
            "chat_id": chat_id,
            "rich_message": {
                **rich_message,
                "is_rtl": True,
                "skip_entity_detection": False,
            },
        },
    )


async def _send_transfer_live(monitor, chat_id: int) -> None:
    for message in _build_messages():
        await _send_rich(monitor, chat_id, message)


def install_transfer_live_command(monitor):
    """Install /transferlive without creating a second Telegram polling loop."""
    original_handle_update = monitor._handle_update
    original_register_commands = monitor._register_commands

    async def handle_update(self, update):
        callback = update.get("callback_query") or {}
        callback_data = str(callback.get("data") or "")
        if callback_data == "transferlive:refresh":
            callback_id = callback.get("id")
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if callback_id:
                await self._api("answerCallbackQuery", {"callback_query_id": callback_id})
            if chat_id is not None and self._is_subscribed(chat_id):
                await _send_transfer_live(self, chat_id)
            return

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

        await _send_transfer_live(self, chat_id)

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
