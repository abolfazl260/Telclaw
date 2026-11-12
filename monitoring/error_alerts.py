"""Bounded Telegram admin alerts for application ERROR/CRITICAL log records.

The normal Python logging handler may be called from worker threads; it must
never depend on asyncio.get_running_loop() in the emitting thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import config

MAX_PENDING_ALERTS = 128
REPEAT_WINDOW_SECONDS = 300
MAX_TRACKED_SIGNATURES = 512


def redact_secrets(value, *, max_length=3000):
    message = str(value)
    credentials = [
        getattr(config, name, "") for name in (
            "TELEGRAM_BOT_TOKEN", "API_HASH", "GROQ_API_KEY",
            "CLOUDFLARE_API_TOKEN", "ADVERTIO_INGEST_KEY",
        )
    ]
    for providers in (
        getattr(config, "GROQ_PROVIDERS", ()),
        getattr(config, "CLOUDFLARE_PROVIDERS", ()),
    ):
        for provider in providers or ():
            credentials.extend(provider.get(key, "") for key in ("api_key", "api_token"))
    for credential in sorted(
        {item for item in credentials if isinstance(item, str) and len(item) >= 6},
        key=len, reverse=True,
    ):
        message = message.replace(credential, "[REDACTED]")
    return message[:max_length]


class ErrorAlertDispatcher:
    """Serialize and throttle alerts without blocking log emitters."""

    def __init__(self, monitor, *, max_pending=MAX_PENDING_ALERTS,
                 repeat_seconds=REPEAT_WINDOW_SECONDS):
        self.monitor = monitor
        self.queue = asyncio.Queue(maxsize=max_pending)
        self.loop = asyncio.get_running_loop()
        self.repeat_seconds = repeat_seconds
        self.last_sent = {}
        self.suppressed = {}
        self.dropped = 0
        self.task = None
        self.stopped = False

    def start(self):
        self.task = self.loop.create_task(self._drain(), name="telclaw-admin-error-alerts")

    def submit(self, level, source, message, context=None):
        """May be called from any thread; dispatch onto the event loop."""
        if self.stopped or self.loop.is_closed():
            return
        safe_context = None
        if isinstance(context, dict):
            original_text = context.get("original_message_text")
            if isinstance(original_text, str) and original_text:
                safe_context = {
                    "original_message_text": redact_secrets(
                        original_text, max_length=4096
                    )
                }
                for key, limit in (
                    ("original_message_id", 64),
                    ("original_channel_username", 128),
                ):
                    value = context.get(key)
                    if value is not None:
                        safe_context[key] = str(value)[:limit]
                link = context.get("original_message_link")
                if (
                    isinstance(link, str)
                    and link.startswith("https://t.me/")
                    and len(link) <= 500
                    and not any(ord(character) < 32 for character in link)
                ):
                    safe_context["original_message_link"] = link

        try:
            self.loop.call_soon_threadsafe(
                self._enqueue, level, source, redact_secrets(message), safe_context
            )
        except RuntimeError:
            # A shutdown race should not interrupt file logging.
            pass

    def _enqueue(self, level, source, message, context=None):
        if self.stopped:
            return
        try:
            self.queue.put_nowait((level, source, message, context))
        except asyncio.QueueFull:
            self.dropped += 1

    async def _drain(self):
        while True:
            level, source, message, context = await self.queue.get()
            try:
                # Deduplicate repeated failures, but keep each source listing's
                # validation warning distinct so its original message is delivered.
                signature = re.sub(
                    r"\b(message_id|record_id|telegram_message_id)\s*=\s*[^\s,|]+",
                    r"\1=*", message, flags=re.IGNORECASE,
                )
                if context and context.get("original_message_id") is not None:
                    signature += (
                        "\noriginal_channel_username="
                        + str(context.get("original_channel_username") or "")
                        + "\noriginal_message_id="
                        + str(context["original_message_id"])
                    )
                key = (level, source, signature)
                now = time.monotonic()
                last = self.last_sent.get(key)
                if last is not None and now - last < self.repeat_seconds:
                    self.suppressed[key] = self.suppressed.get(key, 0) + 1
                    continue
                repeats = self.suppressed.pop(key, 0)
                self.last_sent[key] = now
                if len(self.last_sent) > MAX_TRACKED_SIGNATURES:
                    oldest = sorted(self.last_sent, key=self.last_sent.get)[:128]
                    for old in oldest:
                        self.last_sent.pop(old, None)
                        self.suppressed.pop(old, None)
                if repeats:
                    message += f"\nSimilar errors suppressed: {repeats}"
                if context:
                    await self.monitor.error(level, source, message, context=context)
                else:
                    await self.monitor.error(level, source, message)
                if self.dropped:
                    dropped = self.dropped
                    self.dropped = 0
                    await self.monitor.error(
                        "WARNING", "telclaw.alert_dispatcher",
                        f"{dropped} additional error notifications were dropped while the queue was full.",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The alert path must not generate another ERROR alert itself.
                logging.getLogger(__name__).warning("Admin error alert delivery failed")
            finally:
                self.queue.task_done()

    async def stop(self):
        self.stopped = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
