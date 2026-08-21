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
from typing import Iterable

import aiohttp

import config
from storage import database

logger = logging.getLogger(__name__)


class TelegramMonitor:
    def __init__(self) -> None:
        self.token = config.TELEGRAM_BOT_TOKEN
        self.enabled = config.TELEGRAM_MONITOR_ENABLED and bool(self.token)
        self._task: asyncio.Task | None = None
        self._offset = 0
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Telegram monitor disabled")
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._poll_updates(), name="telegram-monitor-poll")
        logger.info("Telegram monitoring bot started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _api(self, method: str, payload: dict | None = None) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        timeout = aiohttp.ClientTimeout(total=20)
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
            database.subscribe_monitor_chat(
                chat_id=int(chat_id),
                username=chat.get("username"),
                first_name=chat.get("first_name"),
            )
            await self._send(chat_id, "✅ Telclaw monitoring فعال شد.\nاز این پس خطاها و گزارش‌های سیستم برای شما ارسال می‌شود.")
        elif text.startswith("/stop"):
            database.unsubscribe_monitor_chat(int(chat_id))
            await self._send(chat_id, "⛔ دریافت گزارش‌های Telclaw متوقف شد.")

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self._api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        except Exception:
            logger.exception("Telegram monitor failed to send message")

    async def broadcast(self, text: str) -> None:
        if not self.enabled:
            return
        subscribers = database.get_monitor_subscribers()
        if not subscribers:
            return
        for subscriber in subscribers:
            chat_id = int(subscriber["chat_id"])
            try:
                await self._send(chat_id, text)
            except Exception:
                logger.exception("Failed to notify Telegram subscriber %s", chat_id)

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
        titles = {
            "crawl": "📥 CRAWL REPORT",
            "processing": "⚙️ PROCESSING REPORT",
            "ai": "🤖 AI REPORT",
            "advertio": "📤 ADVERTIO REPORT",
        }
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
