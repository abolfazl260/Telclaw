"""Isolated Telegram monitoring/notification service.

This module is intentionally independent from crawler, AI, and Advertio logic.
It uses Telegram Bot API directly and can be disabled completely via env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Optional


class TelegramMonitor:
    def __init__(self) -> None:
        self.enabled = os.getenv("TELCLAW_TELEGRAM_MONITOR_ENABLED", "false").lower() in {
            "1", "true", "yes", "on"
        }
        self.token = os.getenv("TELCLAW_TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELCLAW_TELEGRAM_CHAT_ID", "").strip()
        self.username = os.getenv("TELCLAW_TELEGRAM_CHAT_USERNAME", "").strip()
        self.report_interval = max(
            1, int(os.getenv("TELCLAW_TELEGRAM_REPORT_INTERVAL_MINUTES", "5"))
        )
        self.max_error_history = max(
            10, int(os.getenv("TELCLAW_TELEGRAM_ERROR_HISTORY_SIZE", "100"))
        )
        self._errors: deque[dict] = deque(maxlen=self.max_error_history)
        self._lock = asyncio.Lock()
        self._summary_task: Optional[asyncio.Task] = None

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.token and self.chat_id)

    async def start(self) -> None:
        if not self.configured:
            return
        if self._summary_task is None or self._summary_task.done():
            self._summary_task = asyncio.create_task(self._summary_loop())

    async def stop(self) -> None:
        if self._summary_task and not self._summary_task.done():
            self._summary_task.cancel()
            try:
                await self._summary_task
            except asyncio.CancelledError:
                pass
            self._summary_task = None

    async def send(self, text: str) -> bool:
        if not self.configured:
            return False
        try:
            await asyncio.to_thread(self._send_sync, text)
            return True
        except Exception:
            # Monitoring must never be able to crash the crawler.
            return False

    async def report_error(
        self,
        message: str,
        *,
        level: str = "ERROR",
        source: str = "system",
    ) -> None:
        item = {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "level": level.upper(),
            "source": source,
            "message": message[:3000],
        }
        async with self._lock:
            self._errors.append(item)
        await self.send(
            "🚨 <b>Telclaw System Alert</b>\n"
            f"<b>Level:</b> {self._escape(item['level'])}\n"
            f"<b>Source:</b> {self._escape(item['source'])}\n"
            f"<b>Time:</b> {self._escape(item['time'])}\n\n"
            f"<code>{self._escape(item['message'])}</code>"
        )

    async def report(self, title: str, lines: list[str]) -> None:
        body = "\n".join(self._escape(line) for line in lines)
        await self.send(f"📊 <b>{self._escape(title)}</b>\n\n{body}")

    async def _summary_loop(self) -> None:
        while True:
            await asyncio.sleep(self.report_interval * 60)
            async with self._lock:
                errors = list(self._errors)
            if not errors:
                await self.send("✅ <b>Telclaw Health Report</b>\nNo errors recorded in the current monitoring window.")
                continue
            recent = errors[-20:]
            lines = [f"Errors recorded: <b>{len(errors)}</b>", ""]
            for item in recent:
                lines.append(
                    f"• [{self._escape(item['level'])}] "
                    f"{self._escape(item['source'])}: "
                    f"{self._escape(item['message'][:220])}"
                )
            await self.send("📊 <b>Telclaw Error Report</b>\n\n" + "\n".join(lines))

    def _send_sync(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"Telegram Bot API returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("ok"):
                raise RuntimeError("Telegram Bot API rejected sendMessage")

    @staticmethod
    def _escape(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )


class TelegramLogHandler(logging.Handler):
    """Forward ERROR/CRITICAL log records to Telegram without blocking logging."""

    def __init__(self, monitor: TelegramMonitor) -> None:
        super().__init__(level=logging.ERROR)
        self.monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        if not self.monitor.configured:
            return
        try:
            message = self.format(record)
            loop = _get_running_loop()
            if loop is not None:
                loop.create_task(
                    self.monitor.report_error(
                        message,
                        level=record.levelname,
                        source=record.name or record.filename,
                    )
                )
        except Exception:
            # Never recurse into the logging system from the monitor itself.
            pass


def _get_running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


telegram_monitor = TelegramMonitor()
