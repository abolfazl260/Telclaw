import asyncio

from storage import database
from system_ui import SystemConsoleUI
from monitoring.telegram_monitor import get_telegram_monitor


async def _run():
    database.initialize_db()
    monitor = get_telegram_monitor()
    await monitor.start()
    try:
        await SystemConsoleUI().run()
    finally:
        await monitor.stop()


def main():
    asyncio.run(_run())


if __name__ == "__main__":
    main()
