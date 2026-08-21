"""Independent Telegram Bot API monitor for Telclaw.

The monitor is deliberately isolated from the Telethon crawler client. Users
subscribe with /start and unsubscribe with /stop. Subscribers are persisted
in SQLite so a process restart does not remove them.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone

import aiohttp

import config
from storage import database

logger = logging.getLogger(__name__)


class _TelegramErrorHandler(logging.Handler):
    def __init__(self, monitor: "TelegramMonitor"):
        super().__init__(level=logging.ERROR)
        self.monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("monitoring.telegram_monitor"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.monitor.error(record.levelname, record.name, self.format(record)))


class TelegramMonitor:
    def __init__(self) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.enabled = config.TELEGRAM_MONITOR_ENABLED and bool(self.token)
        self._task: asyncio.Task | None = None
        self._offset = 0
        self._stopping = asyncio.Event()
        self._error_handler: logging.Handler | None = None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Telegram monitor disabled")
            return
        database.initialize_db()
        self._stopping.clear()
        self._error_handler = _TelegramErrorHandler(self)
        self._error_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(self._error_handler)
        await self._register_commands()
        self._task = asyncio.create_task(self._poll_updates(), name="telegram-monitor-poll")
        logger.info("Telegram monitoring bot started")

    async def _register_commands(self) -> None:
        """Register commands shown by Telegram's '/' command suggestions."""
        commands = [
            {"command": "start", "description": "فعال‌سازی دریافت گزارش‌ها"},
            {"command": "stop", "description": "توقف دریافت گزارش‌ها"},
            {"command": "status", "description": "نمایش وضعیت فعلی سیستم"},
        ]
        try:
            await self._api("setMyCommands", {"commands": commands})
            logger.info("Telegram monitor commands registered")
        except Exception:
            # A command-menu failure must never stop the crawler.
            logger.exception("Failed to register Telegram monitor commands")

    async def stop(self) -> None:
        self._stopping.set()
        if self._error_handler is not None:
            logging.getLogger().removeHandler(self._error_handler)
            self._error_handler = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _api(self, method: str, payload: dict | None = None) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload or {}) as response:
                data = await response.json(content_type=None)
                if not response.ok or not data.get("ok"):
                    raise RuntimeError(f"Telegram API {method} failed: HTTP {response.status}")
                return data

    async def _poll_updates(self) -> None:
        while not self._stopping.is_set():
            try:
                result = await self._api("getUpdates", {"offset": self._offset, "timeout": 20, "allowed_updates": ["message"]})
                for update in result.get("result", []):
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram monitor polling failed")
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        text = (message.get("text") or "").strip().lower()
        if text.startswith("/start"):
            database.subscribe_monitor_chat(int(chat_id), chat.get("username"), chat.get("first_name"))
            await self._send(chat_id, "✅ Telclaw monitoring فعال شد.\nاز این پس خطاها و گزارش‌های سیستم برای شما ارسال می‌شود.")
        elif text.startswith("/stop"):
            database.unsubscribe_monitor_chat(int(chat_id))
            await self._send(chat_id, "⛔ دریافت گزارش‌های Telclaw متوقف شد.")
        elif text.startswith("/status"):
            await self._send(chat_id, await self._build_status_message())

    async def _build_status_message(self) -> str:
        status = database.get_pipeline_status()
        return (
            "📊 <b>Telclaw Current Status</b>\n\n"
            f"🟢 <b>System:</b> {html.escape(status['system'])}\n"
            f"📥 <b>Collected:</b> {status['collected']}\n"
            f"⚙️ <b>Processing pending:</b> {status['processing_pending']}\n"
            f"⚙️ <b>Processing failed:</b> {status['processing_failed']}\n"
            f"🤖 <b>AI pending:</b> {status['ai_pending']}\n"
            f"🤖 <b>AI failed:</b> {status['ai_failed']}\n"
            f"📤 <b>Advertio pending:</b> {status['advertio_pending']}\n"
            f"📤 <b>Advertio failed:</b> {status['advertio_failed']}\n"
            f"📦 <b>Total messages:</b> {status['total_messages']}\n"
            f"📡 <b>Channels:</b> {status['channels']}\n"
            f"👥 <b>Active subscribers:</b> {status['subscribers']}\n\n"
            f"🕐 <b>Checked:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

    async def _send(self, chat_id: int, text: str) -> None:
        await self._api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})

    async def broadcast(self, text: str) -> None:
        if not self.enabled:
            return
        for subscriber in database.get_monitor_subscribers():
            try:
                await self._send(int(subscriber["chat_id"]), text)
            except Exception:
                logger.warning("Telegram monitor delivery failed for subscriber %s", subscriber["chat_id"])

    async def error(self, level: str, source: str, message: str) -> None:
        safe = html.escape(message[:3500])
        await self.broadcast(
            f"🚨 <b>Telclaw System Error</b>\n\n"
            f"<b>Level:</b> {html.escape(level)}\n"
            f"<b>Source:</b> {html.escape(source)}\n"
            f"<b>Time:</b> {datetime.now(timezone.utc).isoformat()}\n\n"
            f"<pre>{safe}</pre>"
        )

    async def report(self, kind: str, stats: dict) -> None:
        titles = {"crawl": "📥 CRAWL REPORT", "processing": "⚙️ PROCESSING REPORT", "ai": "🤖 AI REPORT", "advertio": "📤 ADVERTIO REPORT"}
        lines = [f"<b>{html.escape(titles.get(kind, kind.upper() + ' REPORT'))}</b>"]
        for key, value in stats.items():
            lines.append(f"<b>{html.escape(str(key))}:</b> {html.escape(str(value))}")
        await self.broadcast("\n".join(lines))


_monitor: TelegramMonitor | None = None


def get_telegram_monitor() -> TelegramMonitor:
    global _monitor
    if _monitor is None:
        _monitor = TelegramMonitor()
    return _monitor
