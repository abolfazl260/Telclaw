import asyncio
import logging

from storage import database
from system_ui import SystemConsoleUI
from monitoring.telegram_monitor import get_telegram_monitor
from delivery.telegram_transfer_publisher import TelegramTransferPublisher, TransferTelegramPublishError
import config

logger = logging.getLogger("telclaw.transfer_publisher")


async def _transfer_publisher_loop(publisher):
    while True:
        try:
            result = await publisher.publish_pending(limit=50)
            if result["sent"] or result["failed"] or result["rejected"]:
                logger.info(
                    "[TRANSFER TELEGRAM] cycle found=%s sent=%s failed=%s rejected=%s",
                    result["found"], result["sent"], result["failed"], result["rejected"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[TRANSFER TELEGRAM] publisher cycle failed")
        await asyncio.sleep(config.TRANSFER_TELEGRAM_INTERVAL_MINUTES * 60)


async def _run():
    database.initialize_db()
    monitor = get_telegram_monitor()
    await monitor.start()

    transfer_publisher = None
    transfer_task = None
    if config.TRANSFER_TELEGRAM_PUBLISH_ENABLED:
        try:
            transfer_publisher = TelegramTransferPublisher()
            transfer_task = asyncio.create_task(
                _transfer_publisher_loop(transfer_publisher),
                name="telegram-transfer-publisher",
            )
            logger.info(
                "Telegram transfer publisher started | channel=%s | interval=%sm",
                config.TRANSFER_TELEGRAM_CHANNEL,
                config.TRANSFER_TELEGRAM_INTERVAL_MINUTES,
            )
        except TransferTelegramPublishError as exc:
            logger.error("Telegram transfer publisher disabled: %s", exc)

    try:
        await SystemConsoleUI().run()
    finally:
        if transfer_task:
            transfer_task.cancel()
            try:
                await transfer_task
            except asyncio.CancelledError:
                pass
        await monitor.stop()


def main():
    asyncio.run(_run())


if __name__ == "__main__":
    main()
