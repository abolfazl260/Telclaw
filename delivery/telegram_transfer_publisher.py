"""Dedicated Telegram publisher for AI-processed transfer advertisements."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

import aiohttp

import config
from storage import database

logger = logging.getLogger(__name__)


class TransferTelegramPublishError(RuntimeError):
    """Raised when a transfer advertisement cannot be published."""


class TelegramTransferPublisher:
    def __init__(self, *, token=None, channel=None):
        self.token = (token if token is not None else config.TELEGRAM_BOT_TOKEN).strip()
        self.channel = (channel if channel is not None else config.TRANSFER_TELEGRAM_CHANNEL).strip()
        if not self.token:
            raise TransferTelegramPublishError("TELCLAW_TELEGRAM_BOT_TOKEN is required")
        if not self.channel:
            raise TransferTelegramPublishError("TELCLAW_TRANSFER_TELEGRAM_CHANNEL is required")
        database.initialize_db()
        self._ensure_publication_table()

    @staticmethod
    def _ensure_publication_table():
        conn = database.get_connection()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS telegram_transfer_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, message_row_id INTEGER NOT NULL UNIQUE,
                channel_username TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
                telegram_message_id INTEGER, error TEXT, processed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(message_row_id) REFERENCES messages(id) ON DELETE CASCADE)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transfer_publication_status ON telegram_transfer_publications(status)")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
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

    @staticmethod
    def _value(data, key):
        value = data.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _country_flag(value):
        text = str(value or "").strip().lower()
        if re.fullmatch(r"[a-z]{2}", text):
            return "".join(chr(127397 + ord(c)) for c in text.upper())
        flags = {
            "iran":"🇮🇷", "ایران":"🇮🇷", "germany":"🇩🇪", "deutschland":"🇩🇪", "آلمان":"🇩🇪",
            "turkey":"🇹🇷", "türkiye":"🇹🇷", "turkiye":"🇹🇷", "ترکیه":"🇹🇷", "canada":"🇨🇦", "کانادا":"🇨🇦",
            "usa":"🇺🇸", "united states":"🇺🇸", "america":"🇺🇸", "آمریکا":"🇺🇸", "uk":"🇬🇧", "united kingdom":"🇬🇧",
            "england":"🇬🇧", "انگلستان":"🇬🇧", "بریتانیا":"🇬🇧", "france":"🇫🇷", "فرانسه":"🇫🇷", "italy":"🇮🇹",
            "ایتالیا":"🇮🇹", "spain":"🇪🇸", "اسپانیا":"🇪🇸", "netherlands":"🇳🇱", "the netherlands":"🇳🇱", "هلند":"🇳🇱",
            "belgium":"🇧🇪", "بلژیک":"🇧🇪", "austria":"🇦🇹", "اتریش":"🇦🇹", "switzerland":"🇨🇭", "سوئیس":"🇨🇭",
            "sweden":"🇸🇪", "سوئد":"🇸🇪", "norway":"🇳🇴", "نروژ":"🇳🇴", "denmark":"🇩🇰", "دانمارک":"🇩🇰",
            "finland":"🇫🇮", "فنلاند":"🇫🇮", "poland":"🇵🇱", "لهستان":"🇵🇱", "greece":"🇬🇷", "یونان":"🇬🇷",
            "russia":"🇷🇺", "روسیه":"🇷🇺", "ukraine":"🇺🇦", "اوکراین":"🇺🇦", "uae":"🇦🇪", "united arab emirates":"🇦🇪",
            "امارات":"🇦🇪", "qatar":"🇶🇦", "قطر":"🇶🇦", "saudi arabia":"🇸🇦", "عربستان":"🇸🇦", "kuwait":"🇰🇼", "کویت":"🇰🇼",
            "oman":"🇴🇲", "عمان":"🇴🇲", "iraq":"🇮🇶", "عراق":"🇮🇶", "azerbaijan":"🇦🇿", "آذربایجان":"🇦🇿",
            "georgia":"🇬🇪", "گرجستان":"🇬🇪", "armenia":"🇦🇲", "ارمنستان":"🇦🇲", "china":"🇨🇳", "چین":"🇨🇳",
            "japan":"🇯🇵", "ژاپن":"🇯🇵", "south korea":"🇰🇷", "کره جنوبی":"🇰🇷", "india":"🇮🇳", "هند":"🇮🇳",
            "pakistan":"🇵🇰", "پاکستان":"🇵🇰", "afghanistan":"🇦🇫", "افغانستان":"🇦🇫",
        }
        return flags.get(text, "")

    @classmethod
    def _location_line(cls, label, city, country):
        flag = cls._country_flag(country)
        return f"{label}: {flag + ' ' if flag else ''}{city}"

    @staticmethod
    def _number(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _format_weight(cls, data):
        weight = cls._number(data.get("weight"))
        if weight is None:
            return None
        unit = cls._value(data, "weight_unit")
        return f"{weight:g}{(' ' + unit) if unit else ''}"

    @classmethod
    def _format_volume(cls, data):
        volume = cls._number(data.get("volume"))
        if volume is None:
            return None
        return f"{volume:g} {cls._value(data, 'volume_unit') or 'm³'}"

    @classmethod
    def _infer_volume_from_text(cls, data):
        text = " ".join(str(data.get(key) or "") for key in ("description", "title", "features"))
        match = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(m3|m³|cbm|cubic\s*meters?|متر\s*مکعب)(?!\w)", text, re.IGNORECASE)
        if not match:
            return None
        return f"{float(match.group(1).replace(',', '.')):g} m³"

    @staticmethod
    def _transport_type(value):
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {"ground":"زمینی", "land":"زمینی", "road":"زمینی", "truck":"زمینی", "زمینی":"زمینی",
                   "air":"هوایی", "air_freight":"هوایی", "flight":"هوایی", "هوایی":"هوایی",
                   "sea":"دریایی", "marine":"دریایی", "ocean":"دریایی", "دریایی":"دریایی"}
        return aliases.get(text, value)

    @staticmethod
    def _telegram_username(record):
        username = str(record.get("sender_username") or "").strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return None
        return username

    @classmethod
    def _contact_button(cls, record):
        username = cls._telegram_username(record)
        if not username:
            return None
        return {
            "inline_keyboard": [[
                {"text": f"👤 @{username}", "url": f"https://t.me/{username}"}
            ]]
        }

    @classmethod
    def format_ad(cls, record, data):
        origin = cls._value(data, "origin_city")
        destination = cls._value(data, "destination_city")
        if not origin or not destination:
            raise TransferTelegramPublishError("Transfer advertisement requires both origin_city and destination_city")

        transfer_role = cls._value(data, "transfer_role")
        role = {"passenger": "✈️ مسافر", "shipper": "📦 ارسال بار"}.get((transfer_role or "").lower())
        lines = [role] if role else []
        lines.extend([
            cls._location_line("📍 مبدا", origin, cls._value(data, "origin_country")),
            cls._location_line("📍 مقصد", destination, cls._value(data, "destination_country")),
        ])
        cargo = cls._value(data, "cargo_type")
        if cargo:
            lines.append(f"📦 نوع بار: {cargo}")
        weight = cls._format_weight(data)
        if weight:
            lines.append(f"⚖️ وزن: {weight}")
        volume = cls._format_volume(data) or cls._infer_volume_from_text(data)
        if volume:
            lines.append(f"📏 حجم: {volume}")
        departure_date = cls._value(data, "departure_date")
        if departure_date:
            try:
                gregorian = datetime.strptime(departure_date[:10], "%Y-%m-%d").date()
                lines.append(f"📅 تاریخ ارسال: {gregorian.isoformat()} ({cls._jalali(gregorian)})")
            except ValueError:
                lines.append(f"📅 تاریخ ارسال: {departure_date}")
        transport = cls._transport_type(data.get("transport_type"))
        if transport:
            lines.append(f"🚛 نوع حمل: {transport}")
        description = cls._value(data, "description")
        if description:
            lines.append(f"📝 توضیحات: {description}")
        price = cls._number(data.get("price"))
        if price is not None:
            lines.append(f"💰 هزینه: {price:g} {cls._value(data, 'currency') or 'CAD'}")
        contact = cls._value(data, "contact")
        if contact:
            lines.append(f"📞 تماس: {contact}")
        return "\n".join(lines)

    async def _send_message(self, text, reply_markup=None):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.channel, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        timeout = aiohttp.ClientTimeout(total=config.TRANSFER_TELEGRAM_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                data = await response.json(content_type=None)
                if not response.ok or not data.get("ok"):
                    description = data.get("description") if isinstance(data, dict) else None
                    raise TransferTelegramPublishError(f"Telegram sendMessage failed: HTTP {response.status} {description or ''}".strip())
                return data["result"]

    @staticmethod
    def _pending_records(limit=100):
        conn = database.get_connection()
        try:
            rows = conn.execute("""SELECT m.id AS message_row_id, m.channel_username, m.message_id, m.sender_username,
                m.ai_status, m.ai_category, m.raw_text, m.text,
                t.title, t.description, t.origin_city, t.origin_province, t.origin_country,
                t.destination_city, t.destination_province, t.destination_country, t.airline,
                t.flight_number, t.departure_date, t.departure_time, t.arrival_date, t.arrival_time,
                t.transport_type, t.transfer_role, t.cargo_type, t.weight, t.weight_unit, t.quantity,
                t.volume, t.volume_unit, t.price, t.currency, t.contact, t.features
                FROM transferlist t JOIN messages m ON m.id=t.processed_message_id
                LEFT JOIN telegram_transfer_publications p ON p.message_row_id=m.id
                WHERE m.ai_status='processed' AND m.ai_category='transferlist'
                  AND (p.id IS NULL OR p.status='retry') ORDER BY m.date ASC, m.id ASC LIMIT ?""", (int(limit),)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _claim(message_row_id, channel_username):
        conn = database.get_connection()
        try:
            conn.execute("""INSERT INTO telegram_transfer_publications(message_row_id,channel_username,status)
                VALUES(?,?, 'processing') ON CONFLICT(message_row_id) DO UPDATE SET
                channel_username=excluded.channel_username, status='processing', error=NULL""", (message_row_id, channel_username))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _result(message_row_id, status, telegram_message_id=None, error=None):
        conn = database.get_connection()
        try:
            conn.execute("""UPDATE telegram_transfer_publications SET status=?, telegram_message_id=?, error=?, processed_at=?
                WHERE message_row_id=?""", (status, telegram_message_id, error, datetime.utcnow().isoformat() + "Z", message_row_id))
            conn.commit()
        finally:
            conn.close()

    async def publish_pending(self, limit=100):
        records = self._pending_records(limit)
        result = {"found": len(records), "sent": 0, "failed": 0, "rejected": 0}
        for record in records:
            self._claim(record["message_row_id"], record["channel_username"])
            data = {key: record.get(key) for key in (
                "title", "description", "origin_city", "origin_province", "origin_country",
                "destination_city", "destination_province", "destination_country", "airline",
                "flight_number", "departure_date", "departure_time", "arrival_date", "arrival_time",
                "transport_type", "transfer_role", "cargo_type", "weight", "weight_unit", "quantity",
                "volume", "volume_unit", "price", "currency", "contact", "features")}
            try:
                sent = await self._send_message(self.format_ad(record, data), self._contact_button(record))
                self._result(record["message_row_id"], "sent", telegram_message_id=sent.get("message_id"))
                result["sent"] += 1
                logger.info("[TRANSFER TELEGRAM] sent message=%s telegram_message_id=%s", record.get("message_id"), sent.get("message_id"))
            except TransferTelegramPublishError as exc:
                status = "rejected" if "requires both origin_city" in str(exc) else "retry"
                self._result(record["message_row_id"], status, error=str(exc)[:4000])
                result["rejected" if status == "rejected" else "failed"] += 1
                logger.warning("[TRANSFER TELEGRAM] %s message=%s reason=%s", status, record.get("message_id"), exc)
            except Exception as exc:
                self._result(record["message_row_id"], "retry", error=str(exc)[:4000])
                result["failed"] += 1
                logger.exception("[TRANSFER TELEGRAM] failed message=%s", record.get("message_id"))
        return result
