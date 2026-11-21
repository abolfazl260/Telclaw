"""One loop-owned guard per borrowed Telethon client; never creates a session."""

import asyncio
from weakref import WeakKeyDictionary

_locks = WeakKeyDictionary()


def session_guard(client):
    try:
        lock = _locks.get(client)
    except TypeError:
        # Stateless collection test doubles can omit a real Telethon client.
        return asyncio.Lock()
    if lock is None:
        lock = _locks[client] = asyncio.Lock()
    return lock
