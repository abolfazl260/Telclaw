import asyncio
import logging

from storage import database
from system_ui import SystemConsoleUI
from monitoring.telegram_monitor import TelegramLogHandler, telegram_monitor


async def _run() -> None:
    database.initialize_db()

    if telegram_monitor.configured:
        root_logger = logging.getLogger()
        root_logger.addHandler(TelegramLogHandler(telegram_monitor))
        await telegram_monitor.start()
        await telegram_monitor.send("🟢 <b>Telclaw started</b>\nTelegram system monitoring is active.")

    try:
        await SystemConsoleUI().run()
    finally:
        await telegram_monitor.stop()


def main():
    asyncio.run(_run())


if __name__ == "__main__":
    main()
