import asyncio
import logging
import ssl

from aiohttp import web

from storage import database
from system_ui import SystemConsoleUI
from monitoring.telegram_monitor import get_telegram_monitor
from monitoring.transfer_live import install_transfer_live_command
from delivery.telegram_transfer_publisher import TelegramTransferPublisher, TransferTelegramPublishError
from routed_publisher import RoutedPublisher
from backoffice_web import create_app, public_origin
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
    backoffice_ready = False
    if config.BACKOFFICE_ENABLED:
        try:
            public_origin()
            if not config.TELEGRAM_BOT_TOKEN:
                raise RuntimeError("TELCLAW_TELEGRAM_BOT_TOKEN is required")
            if bool(config.BACKOFFICE_TLS_CERT) != bool(config.BACKOFFICE_TLS_KEY):
                raise RuntimeError("Both back office TLS certificate and key must be set")
            ssl_context = None
            if config.BACKOFFICE_TLS_CERT:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
                ssl_context.load_cert_chain(config.BACKOFFICE_TLS_CERT, config.BACKOFFICE_TLS_KEY)
            backoffice_runner = web.AppRunner(create_app())
            await backoffice_runner.setup()
            await web.TCPSite(backoffice_runner, config.BACKOFFICE_HOST, config.BACKOFFICE_PORT, ssl_context=ssl_context).start()
            backoffice_ready = True
            logger.info("Back office listening at %s:%s", config.BACKOFFICE_HOST, config.BACKOFFICE_PORT)
        except Exception as exc:
            if backoffice_runner:
                await backoffice_runner.cleanup()
                backoffice_runner = None
            logger.exception("Back office disabled; Telegram monitoring will still start: %s", exc)
            print(f"[BACK OFFICE] Unavailable: {exc}. Telegram monitoring will still start.")
    monitor = get_telegram_monitor()
    install_transfer_live_command(monitor)
    await monitor.start()
    if not monitor.enabled:
        print("[TELEGRAM MONITOR] Disabled. Set TELCLAW_TELEGRAM_MONITOR_ENABLED=true and a bot token in .env.")

    transfer_publisher = None
    transfer_task = None
    if backoffice_ready or (not config.BACKOFFICE_ENABLED and config.TRANSFER_TELEGRAM_PUBLISH_ENABLED):
        try:
            transfer_publisher = RoutedPublisher() if backoffice_ready else TelegramTransferPublisher()
            transfer_task = asyncio.create_task(
                _transfer_publisher_loop(transfer_publisher),
                name="telegram-transfer-publisher",
            )
            logger.info(
                "Telegram transfer publisher started | channel=%s | interval=%sm",
                'managed rules' if backoffice_ready else config.TRANSFER_TELEGRAM_CHANNEL,
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
