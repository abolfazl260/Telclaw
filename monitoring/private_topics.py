"""Optional, admin-only private topic routing for the Telegram monitoring bot.

The settings apply bot-wide, but only explicitly authorized administrators are
provisioned and routed. No topics or existing messages are ever deleted.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging

from storage import database

logger = logging.getLogger(__name__)

TOPICS = (
    ("dashboard", "داشبورد و مدیریت"),
    ("warning", "هشدارها"),
    ("error", "خطاها"),
)
_TOPIC_SETTINGS = {
    "has_topics_enabled": True,
    "has_topic_sidebar": True,
    "allows_users_to_create_topics": False,
}


class PrivateTopics:
    def __init__(self, monitor, *, enabled: bool = False):
        self.monitor = monitor
        self.enabled = bool(enabled)
        self.ready = False
        self._schema_ready = False
        self._cache: dict[tuple[int, str], int] = {}
        self._lock = asyncio.Lock()
        self._reply_thread: ContextVar[int | None] = ContextVar(
            "private_topic_reply", default=None
        )
        self._route_key: ContextVar[str | None] = ContextVar(
            "private_topic_route", default=None
        )

    @contextmanager
    def reply_in_thread(self, thread_id):
        try:
            candidate = int(thread_id)
            thread_id = candidate if candidate > 0 else None
        except (TypeError, ValueError):
            thread_id = None
        token = self._reply_thread.set(thread_id)
        try:
            yield
        finally:
            self._reply_thread.reset(token)

    @contextmanager
    def route(self, key: str):
        token = self._route_key.set(key if key in dict(TOPICS) else "dashboard")
        try:
            yield
        finally:
            self._route_key.reset(token)

    async def configure(self):
        """Run once on startup, and only if explicitly enabled in config."""
        if not self.enabled:
            return
        try:
            response = await self.monitor._api("getMe", {})
            current = response.get("result") or {}
            if not all(current.get(key) is value for key, value in _TOPIC_SETTINGS.items()):
                await self.monitor._api("setMySettings", dict(_TOPIC_SETTINGS))
            self.ready = True
        except Exception as exc:
            # Optional topic support must not prevent monitoring startup.
            logger.warning("Private Topic Sidebar unavailable: %s", exc)
            self.ready = False

    def _ensure_schema(self):
        if self._schema_ready:
            return
        conn = database.get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telegram_monitor_private_topics (
                    chat_id INTEGER NOT NULL,
                    topic_key TEXT NOT NULL,
                    message_thread_id INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, topic_key)
                )
            """)
            conn.commit()
            self._schema_ready = True
        finally:
            conn.close()

    def _lookup(self, chat_id: int, key: str):
        identity = (int(chat_id), key)
        if identity in self._cache:
            return self._cache[identity]
        self._ensure_schema()
        conn = database.get_connection()
        try:
            record = conn.execute(
                "SELECT message_thread_id FROM telegram_monitor_private_topics "
                "WHERE chat_id = ? AND topic_key = ?", identity,
            ).fetchone()
        finally:
            conn.close()
        if record:
            thread = int(record[0])
            self._cache[identity] = thread
            return thread
        return None

    def _save(self, chat_id: int, key: str, thread_id: int):
        self._ensure_schema()
        conn = database.get_connection()
        try:
            conn.execute(
                "INSERT INTO telegram_monitor_private_topics "
                "(chat_id, topic_key, message_thread_id) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, topic_key) DO UPDATE SET "
                "message_thread_id = excluded.message_thread_id",
                (int(chat_id), key, int(thread_id)),
            )
            conn.commit()
            self._cache[(int(chat_id), key)] = int(thread_id)
        finally:
            conn.close()

    def invalidate_thread(self, chat_id: int, thread_id: int):
        """Forget a confirmed-deleted thread; the next /start reprovisions it."""
        self._ensure_schema()
        chat_id, thread_id = int(chat_id), int(thread_id)
        conn = database.get_connection()
        try:
            conn.execute(
                "DELETE FROM telegram_monitor_private_topics "
                "WHERE chat_id = ? AND message_thread_id = ?", (chat_id, thread_id),
            )
            conn.commit()
        finally:
            conn.close()
        for identity, cached in list(self._cache.items()):
            if identity[0] == chat_id and cached == thread_id:
                del self._cache[identity]

    async def ensure_for_admin(self, chat_id: int):
        if not self.ready or not self.monitor._is_admin_chat(chat_id):
            return
        async with self._lock:
            for key, title in TOPICS:
                if self._lookup(chat_id, key) is not None:
                    continue
                response = await self.monitor._api(
                    "createForumTopic", {"chat_id": int(chat_id), "name": title}
                )
                thread_id = int(response["result"]["message_thread_id"])
                if thread_id <= 0:
                    raise ValueError("Telegram returned an invalid topic ID")
                self._save(chat_id, key, thread_id)

    def target_thread(self, chat_id: int):
        if not self.ready or not self.monitor._is_admin_chat(chat_id):
            return None
        forced = self._route_key.get()
        try:
            if forced is not None:
                return self._lookup(chat_id, forced)
            incoming = self._reply_thread.get()
            if incoming is not None:
                return incoming
            return self._lookup(chat_id, "dashboard")
        except Exception as exc:
            logger.warning("Unable to resolve Telegram topic for chat %s: %s", chat_id, exc)
            return None

    @staticmethod
    def is_invalid_thread(exc):
        """Retry only definite HTTP 400 invalid-topic responses, never timeouts."""
        if getattr(exc, "status", None) != 400:
            return False
        detail = str(exc).lower()
        return any(part in detail for part in (
            "message thread not found", "topic not found", "topic_id_invalid",
            "message thread id is invalid", "message_thread_id_invalid",
        ))
