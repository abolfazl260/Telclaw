"""Single-reader asynchronous terminal input for interactive menus and Q-stop tasks.

A daemon thread owns stdin for the lifetime of the process.  Canceling an
async waiter never leaves an old input() call competing with the next menu.
"""

import asyncio
from collections import deque
import threading


class ConsoleBack(Exception):
    """The operator entered Q in a menu or input form."""


_lines = deque()
_lock = threading.Lock()
_waiter = None
_reader_thread = None
_end_of_input = False


def _wake(waiter):
    if waiter is None:
        return
    loop, future = waiter

    def notify():
        if not future.done():
            future.set_result(None)

    try:
        loop.call_soon_threadsafe(notify)
    except RuntimeError:
        # The owning event loop has already shut down.
        pass


def _read_stdin():
    global _end_of_input
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            with _lock:
                _end_of_input = True
                waiter = _waiter
            _wake(waiter)
            return

        with _lock:
            _lines.append(line)
            waiter = _waiter
        _wake(waiter)


async def read_line(prompt=""):
    """Read a line without starting a new stdin thread for each prompt."""
    global _reader_thread, _waiter
    if prompt:
        print(prompt, end="", flush=True)

    loop = asyncio.get_running_loop()
    while True:
        with _lock:
            if _lines:
                return _lines.popleft()
            if _end_of_input:
                raise EOFError("Terminal input closed")
            if _waiter is not None:
                raise RuntimeError("Terminal already has an active input prompt")

            future = loop.create_future()
            waiter = (loop, future)
            _waiter = waiter
            if _reader_thread is None:
                _reader_thread = threading.Thread(
                    target=_read_stdin, name="telclaw-terminal-input", daemon=True
                )
                _reader_thread.start()

        try:
            await future
        finally:
            with _lock:
                if _waiter is waiter:
                    _waiter = None


def is_q(value):
    return str(value).strip().casefold() == "q"
