"""Shared bot-wide pacing/cooldown; no publisher queues or scheduling state."""

import asyncio
from contextlib import closing
import hashlib
import math
import sqlite3
import time

from storage import database


class BotCooldown(RuntimeError):
    def __init__(self, seconds):
        self.seconds = max(1, math.ceil(seconds))
        super().__init__("Telegram bot is in shared cooldown")


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS telegram_bot_limits (
        bot_key TEXT NOT NULL, scope TEXT NOT NULL, next_at REAL NOT NULL DEFAULT 0,
        cooldown REAL NOT NULL DEFAULT 0, PRIMARY KEY(bot_key,scope))""")


def key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def cooldown_seconds(token):
    try:
        with closing(database.get_connection()) as conn:
            row = conn.execute("SELECT cooldown FROM telegram_bot_limits WHERE bot_key=? AND scope='bot'", (key(token),)).fetchone()
            return max(0, math.ceil(row[0] - time.time())) if row else 0
    except sqlite3.Error:
        # Compatibility for isolated legacy clients without application bootstrap.
        return 0


def note_429(token, seconds):
    try:
        delay = max(1, min(int(seconds), 604800))
    except (ValueError, TypeError):
        delay = 60
    try:
        with closing(database.get_connection()) as conn, conn:
            conn.execute("""INSERT INTO telegram_bot_limits(bot_key,scope,cooldown) VALUES(?,'bot',?)
                ON CONFLICT(bot_key,scope) DO UPDATE SET cooldown=MAX(cooldown,excluded.cooldown)""",
                (key(token), time.time() + delay))
    except sqlite3.Error:
        pass
    return delay


def reserve(token, chat_id=None, cost=1):
    """Reserve one fair slot atomically, sleeping outside the SQLite transaction."""
    try:
        with closing(database.get_connection()) as conn:
            conn.execute("PRAGMA busy_timeout=250")
            conn.execute("BEGIN IMMEDIATE")
            now, fingerprint = time.time(), key(token)
            bot = conn.execute("SELECT next_at,cooldown FROM telegram_bot_limits WHERE bot_key=? AND scope='bot'", (fingerprint,)).fetchone()
            if bot and bot[1] > now:
                raise BotCooldown(bot[1] - now)
            scopes = [("bot", .05 * max(1, cost))]
            if chat_id is not None:
                scopes.append(("chat:" + str(chat_id), 1.1))
            slot = max(now, bot[0] if bot else now)
            for scope, _ in scopes[1:]:
                row = conn.execute("SELECT next_at FROM telegram_bot_limits WHERE bot_key=? AND scope=?", (fingerprint, scope)).fetchone()
                if row:
                    slot = max(slot, row[0])
            for scope, spacing in scopes:
                conn.execute("""INSERT INTO telegram_bot_limits(bot_key,scope,next_at) VALUES(?,?,?)
                    ON CONFLICT(bot_key,scope) DO UPDATE SET next_at=excluded.next_at""", (fingerprint, scope, slot + spacing))
            conn.commit()
            return max(0, slot - now)
    except sqlite3.Error as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise BotCooldown(1) from exc


async def wait_turn(token, chat_id=None, cost=1):
    delay = reserve(token, chat_id, cost)
    if delay:
        await asyncio.sleep(delay)
    remaining = cooldown_seconds(token)
    if remaining:
        raise BotCooldown(remaining)
