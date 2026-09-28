import asyncio
import logging
from urllib.parse import urlparse

from aiohttp import web

from storage import database
from system_ui import SystemConsoleUI
from monitoring.telegram_monitor import get_telegram_monitor
from monitoring.transfer_live import install_transfer_live_command
from delivery.telegram_transfer_publisher import TelegramTransferPublisher, TransferTelegramPublishError
from routed_publisher import RoutedPublisher
from backoffice_web import create_app
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
    backoffice_runner = None
    if config.BACKOFFICE_ENABLED:
        url = urlparse(config.BACKOFFICE_PUBLIC_URL)
        if url.scheme != "https" or not url.netloc:
            raise RuntimeError("Back office requires TELCLAW_BACKOFFICE_PUBLIC_URL with HTTPS")
        if not config.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("Back office requires TELCLAW_TELEGRAM_BOT_TOKEN")
        backoffice_runner = web.AppRunner(create_app())
        await backoffice_runner.setup()
        try:
            await web.TCPSite(backoffice_runner, config.BACKOFFICE_HOST, config.BACKOFFICE_PORT).start()
        except Exception:
            await backoffice_runner.cleanup()
            raise
        logger.info("Back office listening at %s:%s", config.BACKOFFICE_HOST, config.BACKOFFICE_PORT)
    monitor = get_telegram_monitor()
    install_transfer_live_command(monitor)
    await monitor.start()

    transfer_publisher = None
    transfer_task = None
    if config.BACKOFFICE_ENABLED or config.TRANSFER_TELEGRAM_PUBLISH_ENABLED:
        try:
            transfer_publisher = RoutedPublisher() if config.BACKOFFICE_ENABLED else TelegramTransferPublisher()
            transfer_task = asyncio.create_task(
                _transfer_publisher_loop(transfer_publisher),
                name="telegram-transfer-publisher",
            )
            logger.info(
                "Telegram transfer publisher started | channel=%s | interval=%sm",
                'managed rules' if config.BACKOFFICE_ENABLED else config.TRANSFER_TELEGRAM_CHANNEL,
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
        if backoffice_runner:
            await backoffice_runner.cleanup()


def main():
    asyncio.run(_run())


if __name__ == "__main__":
    main()
