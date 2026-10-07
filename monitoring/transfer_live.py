"""Telegram /transferlive command using Telegram Bot API Rich Messages."""
from __future__ import annotations

import html
import sqlite3
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from types import MethodType
from zoneinfo import ZoneInfo

from storage import database

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
MAX_RICH_MESSAGE_LENGTH = 30000
COUNTRY_NAMES = {
    "IR": "Iran", "DE": "Germany", "TR": "Turkey", "CA": "Canada", "US": "United States",
    "GB": "United Kingdom", "FR": "France", "IT": "Italy", "ES": "Spain", "NL": "Netherlands",
    "BE": "Belgium", "AT": "Austria", "CH": "Switzerland", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "PL": "Poland", "GR": "Greece", "RU": "Russia",
    "UA": "Ukraine", "AE": "United Arab Emirates", "QA": "Qatar", "SA": "Saudi Arabia", "KW": "Kuwait",
    "OM": "Oman", "IQ": "Iraq", "AZ": "Azerbaijan", "GE": "Georgia", "AM": "Armenia",
    "CN": "China", "JP": "Japan", "KR": "South Korea", "IN": "India", "PK": "Pakistan",
    "AF": "Afghanistan",
}


def _country_flag(iso2: str) -> str:
    code = str(iso2 or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🌍"
    return "".join(chr(127397 + ord(char)) for char in code)


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
        jalali = _jalali(parsed)
        # Show only month/day; hide the year in both calendar columns.
        gregorian_label = parsed.strftime("%d/%m")
        jalali_parts = jalali.split("/")
        jalali_label = f"{jalali_parts[2]}/{jalali_parts[1]}"
        return gregorian_label, jalali_label, parsed
    except ValueError:
        return text[:10] or "-", "-", None


def _origin_label(row) -> str:
    country = str(row["origin_country"] or "").strip().upper()
    city = str(row["origin_city"] or "").strip()
    country_name = COUNTRY_NAMES.get(country, country) or "نامشخص"
    return f"{_country_flag(country)} {country_name}" if country else (city or "نامشخص")


def _destination_label(row) -> str:
    country = str(row["destination_country"] or "").strip().upper()
    city = str(row["destination_city"] or "").strip() or "نامشخص"
    # Rich Message tables are RTL. Use an LTR isolate around the flag+city
    # so the country flag stays visually before the city instead of being
    # reordered by the surrounding RTL layout.
    if country:
        return f"\u2066{_country_flag(country)} {city}\u2069"
    return city


def _remaining_label(departure: date | None, today: date) -> str:
    if departure is None:
        return "—"
    delta = (departure - today).days
    if delta < 0:
        return "منقضی"
    if delta == 0:
        return "امروز"
    if delta == 1:
        return "فردا"
    return f"{delta} روز"


def _transfer_identity(row) -> tuple:
    """Return the identity of one active transfer trip for report deduplication.

    Sender ID is preferred over username because usernames can change and may be absent.
    Route + departure date are part of the identity so one user can legitimately have
    multiple different trips.
    """
    sender_id = row["sender_id"]
    sender_key = f"id:{sender_id}" if sender_id is not None else f"username:{str(row['sender_username'] or '').strip().lower()}"
    origin_country = str(row["origin_country"] or "").strip().casefold()
    origin_city = str(row["origin_city"] or "").strip().casefold()
    destination_country = str(row["destination_country"] or "").strip().casefold()
    destination_city = str(row["destination_city"] or "").strip().casefold()
    departure_date = str(row["departure_date"] or "").strip()[:10]
    return sender_key, origin_country, origin_city, destination_country, destination_city, departure_date


def _fetch_active_rows():
    today = datetime.now(TEHRAN_TZ).date().isoformat()
    conn = database.get_connection()
    try:
        rows = conn.execute(
            """SELECT t.origin_country, t.origin_city, t.destination_country, t.destination_city,
                      t.departure_date, m.sender_id, m.sender_username, m.id AS message_row_id
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

        unique_rows = []
        seen = set()
        for row in rows:
            identity = _transfer_identity(row)
            if identity in seen:
                continue
            seen.add(identity)
            unique_rows.append(row)
        return unique_rows
    finally:
        conn.close()


def _country_summary_rows(rows):
    """Aggregate active transfer requests by origin and destination country."""
    counts = {}
    for row in rows:
        origin = str(row["origin_country"] or "").strip().upper() or "??"
        destination = str(row["destination_country"] or "").strip().upper() or "??"
        # Requests without a known origin or destination are not actionable
        # country-summary rows and must not affect the displayed total.
        if origin == "??" or destination == "??":
            continue
        key = (origin, destination)
        counts[key] = counts.get(key, 0) + 1

    return sorted(
        counts.items(),
        key=lambda item: item[0],
    )


def _fetch_active_country_summary():
    """Return active requests grouped by origin/destination country."""
    return _country_summary_rows(_fetch_active_rows())


def _fetch_published_transfer_stats() -> dict:
    """Return the latest flight number and recent publication counts."""
    conn = database.get_connection()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "telegram_transfer_publications" not in tables:
            return {"latest_number": 0, "last_7_days": 0, "last_30_days": 0}
        now = datetime.now(timezone.utc)
        since_7 = (now - timedelta(days=7)).isoformat()
        since_30 = (now - timedelta(days=30)).isoformat()
        row = conn.execute(
            """SELECT COALESCE(MAX(ad_number), 0) AS latest_number,
                      SUM(CASE WHEN processed_at >= ? THEN 1 ELSE 0 END) AS last_7_days,
                      SUM(CASE WHEN processed_at >= ? THEN 1 ELSE 0 END) AS last_30_days
                 FROM telegram_transfer_publications
                WHERE status='sent'""",
            (since_7, since_30),
        ).fetchone()
        return {
            "latest_number": int(row["latest_number"] or 0),
            "last_7_days": int(row["last_7_days"] or 0),
            "last_30_days": int(row["last_30_days"] or 0),
        }
    except sqlite3.OperationalError:
        return {"latest_number": 0, "last_7_days": 0, "last_30_days": 0}
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
                )
            }
        ]

    total = len(rows)
    generated = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M")

    prefix = (
        "<h1>🟢 آگهی های فعال</h1>"
        f"<p><b>📊 خلاصه وضعیت</b> — <b>{total}</b> آگهی در <b>{len(groups)}</b> مبدا</p>"
        "<hr/>"
    )
    suffix = (
        "<hr/>"
        f"<p>🕐 <b>آخرین بروزرسانی:</b> {generated} تهران</p>"
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


def _build_country_messages():
    """Build Rich Messages for the active request country summary."""
    summary = _fetch_active_country_summary()
    published = _fetch_published_transfer_stats()
    generated = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M")
    if not summary:
        return [{"html": (
            "<h1>Advertio Cargo &amp; Passenger Requests</h1>"
            "<p>📤 Latest published flight number: <b>{:,}</b></p>".format(published["latest_number"])
            + "<p>📅 Published in the last 7 days: <b>{:,}</b></p>".format(published["last_7_days"])
            + "<p>📅 Published in the last 30 days: <b>{:,}</b></p>".format(published["last_30_days"])
            + "<p><b>@advertio_cargo</b></p>"
            "<p>⚠️ No open transfer requests at the moment.</p>"
        )}]

    total = sum(count for _, count in summary)
    rows = [
        "<tr>"
        + _cell("Origin", header=True)
        + _cell("Destination", header=True)
        + _cell("Open Requests", header=True, align="center")
        + "</tr>"
    ]
    for (origin, destination), count in summary:
        origin_name = COUNTRY_NAMES.get(origin, origin if origin != "??" else "نامشخص")
        destination_name = COUNTRY_NAMES.get(destination, destination if destination != "??" else "نامشخص")
        rows.append(
            "<tr>"
            + _cell(f"{_country_flag(origin)} {origin_name}")
            + _cell(f"{_country_flag(destination)} {destination_name}")
            + _cell(f"{count:,}", align="center")
            + "</tr>"
        )

    return [{"html": (
        "<h1>Advertio Cargo &amp; Passenger Requests</h1>"
        f"<p>📊 Total active requests: <b>{total:,}</b></p>"
        f"<p>📤 Latest published flight number: <b>{published['latest_number']:,}</b></p>"
        f"<p>📅 Published in the last 7 days: <b>{published['last_7_days']:,}</b></p>"
        f"<p>📅 Published in the last 30 days: <b>{published['last_30_days']:,}</b></p>"
        "<table bordered striped compact>"
        + "".join(rows)
        + "</table>"
        f"<p>🕐 Last updated: {generated} Tehran</p>"
        "<p><b>@advertio_cargo</b></p>"
    )}]


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


async def _send_transfer_country(monitor, chat_id: int) -> None:
    for message in _build_country_messages():
        await _send_rich(monitor, chat_id, message)


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
        if command not in {"/transferlive", "/transfercountry"}:
            return await original_handle_update(update)

        if not self._is_admin_private_chat(chat, message.get("from")):
            return
        if not self._is_subscribed(chat_id):
            await self._send(chat_id, "⛔ ابتدا با /start دریافت گزارش‌های Telclaw را فعال کنید.")
            return

        if command == "/transfercountry":
            await _send_transfer_country(self, chat_id)
        else:
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
            {"command": "backoffice", "description": "Open private publishing back office"},
            {"command": "transferlive", "description": "نمایش آگهی‌های فعال حمل‌ونقل"},
            {"command": "transfercountry", "description": "خلاصه درخواست‌ها بر اساس کشورهای مبدا و مقصد"},
        ]
        try:
            await self._api("setMyCommands", {"commands": commands})
        except Exception:
            pass

    monitor._handle_update = MethodType(handle_update, monitor)
    monitor._register_commands = MethodType(register_commands, monitor)
    return monitor
