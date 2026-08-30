"""System menu extensions for independently running database-backed queues."""

import asyncio

from colorama import Fore

from ai.ai_service import AIProcessingService
from ai.classification_service import CategoryClassificationService
from ai.groq_connection_test import test_groq_connection
from collection.media_downloader import download_photo_for_record
from delivery.advertio_service import AdvertioDeliveryService, AdvertioMappingError
from services.processing_service import ProcessingService
import config
from ui import ConsoleUI


class SystemConsoleUI(ConsoleUI):
    """Console UI with manual controls for the independent pipeline queues."""

    def __init__(self, processing_service=None, classification_service=None, ai_service=None, advertio_service=None, **kwargs):
        super().__init__(**kwargs)
        self.processing_service = processing_service or ProcessingService()
        self.classification_service = classification_service or CategoryClassificationService()
        self.ai_service = ai_service or AIProcessingService()
        self.advertio_service = advertio_service
        if self.advertio_service is None and config.ADVERTIO_INGEST_ENABLED:
            try:
                self.advertio_service = AdvertioDeliveryService()
            except AdvertioMappingError:
                self.advertio_service = None

    def _make_sync_media_downloader(self):
        """Bridge the async Telethon media downloader into the AI worker thread."""
        if self.client is None:
            return None
        loop = asyncio.get_running_loop()

        def download(record):
            future = asyncio.run_coroutine_threadsafe(
                download_photo_for_record(self.client, record), loop
            )
            return future.result()

        return download

    async def run_processing_queue(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Information Processing Queue")
        self.show_message("Checking the processing queue in the database...", Fore.CYAN)
        try:
            result = self.processing_service.process_pending_with_stats()
            self.show_message(
                f"Completed. Found: {result['found']} | "
                f"Processed: {result['processed']} | Failed: {result['failed']}",
                Fore.GREEN if result["failed"] == 0 else Fore.YELLOW,
            )
        except Exception as exc:
            self.show_message(f"Processing queue failed: {exc}", Fore.RED)
        await self.pause()

    async def run_ai_queue(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("AI Processing Queue")
        self.show_message("Checking the AI queue in the database...", Fore.CYAN)
        try:
            client = await self.connect_client()
            if client is None:
                self.show_message(
                    "AI queue requires a connected Telegram account because housing media may need to be downloaded.",
                    Fore.RED,
                )
                await self.pause()
                return
            self.ai_service.set_media_downloader(self._make_sync_media_downloader())
            result = self.ai_service.process_pending_with_stats()
            if result.get("disabled"):
                self.show_message("AI extraction is disabled in configuration.", Fore.YELLOW)
            else:
                self.show_message(
                    f"Completed. Found: {result['found']} | "
                    f"Processed: {result['processed']} | Failed: {result['failed']} | "
                    f"Skipped: {result['skipped']}",
                    Fore.GREEN if result["failed"] == 0 else Fore.YELLOW,
                )
        except Exception as exc:
            self.show_message(f"AI queue failed: {exc}", Fore.RED)
        await self.pause()

    def show_classification_queue_summary(self):
        """Render a fresh, database-backed snapshot of the classification queue."""
        status = self.classification_service.repository.get_classification_queue_status()
        self.show_section_header("AI Category Classification")
        print(f"{Fore.GREEN}│  Pending:     {status['pending']}")
        print(f"{Fore.GREEN}│  Processing:  {status['processing']}")
        print(f"{Fore.GREEN}│  Classified:  {status['classified']}")
        print(f"{Fore.GREEN}│  Failed:      {status['failed']}")
        self.show_section_footer()
        return status

    async def _prompt_classification_batch_size(self):
        value = await self.prompt_text(
            "Batch size",
            default=str(config.AI_CLASSIFICATION_BATCH_SIZE),
            allow_empty=False,
        )
        try:
            batch_size = int(value)
            if batch_size <= 0:
                raise ValueError
            return batch_size
        except ValueError:
            self.show_message("Batch size must be a positive integer.", Fore.RED)
            return None

    async def run_classification_queue(self):
        batch_size = await self._prompt_classification_batch_size()
        if batch_size is None:
            await self.pause()
            return
        self.show_message("Sending ready messages to AI for category classification...", Fore.CYAN)
        try:
            result = self.classification_service.process_pending_with_stats(limit=batch_size)
            if result.get("disabled"):
                self.show_message("AI category classification is disabled in configuration.", Fore.YELLOW)
                self.show_message("Set TELCLAW_AI_CLASSIFICATION_ENABLED=true to enable it.", Fore.YELLOW)
            else:
                color = Fore.GREEN if result["failed"] == 0 else Fore.YELLOW
                self.show_message(
                    f"Completed. Found: {result['found']} | Classified: {result['processed']} | "
                    f"Failed: {result['failed']} | Skipped: {result['skipped']}",
                    color,
                )
        except Exception as exc:
            self.show_message(f"Classification queue failed: {exc}", Fore.RED)
        self.show_classification_queue_summary()
        await self.pause()

    async def retry_failed_classifications(self):
        retried = self.classification_service.repository.retry_failed_classifications()
        if retried == 0:
            self.show_message("No failed classifications are waiting to be retried.", Fore.YELLOW)
            await self.pause()
            return
        self.show_message(f"Requeued {retried} failed classification(s).", Fore.GREEN)
        await self.run_classification_queue()

    async def classification_settings(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Classification Settings")
        print(f"{Fore.GREEN}│  Enabled: {config.AI_CLASSIFICATION_ENABLED}")
        print(f"{Fore.GREEN}│  Default batch size: {config.AI_CLASSIFICATION_BATCH_SIZE}")
        print(f"{Fore.GREEN}│  Maximum retries: {config.AI_CLASSIFICATION_MAX_RETRIES}")
        self.show_section_footer()
        self.show_message("Set TELCLAW_AI_CLASSIFICATION_BATCH_SIZE in configuration to change the default.", Fore.CYAN)
        await self.pause()

    async def classification_menu(self):
        """Open the manual controls for the independent AI classification queue."""
        while True:
            self.clear_screen()
            self.show_banner()
            self.show_classification_queue_summary()
            print(f"{Fore.GREEN}│  1. Start Classification")
            print(f"{Fore.GREEN}│  2. View Queue Status")
            print(f"{Fore.GREEN}│  3. Retry Failed")
            print(f"{Fore.GREEN}│  4. Back")
            print(f"{Fore.GREEN}│  5. Classification Settings")
            self.show_section_footer()
            choice = await self.prompt_choice("\nChoose an option [1-5]: ", {"1", "2", "3", "4", "5"})
            if choice == "1":
                await self.run_classification_queue()
            elif choice == "2":
                self.clear_screen()
                self.show_banner()
                self.show_classification_queue_summary()
                await self.pause()
            elif choice == "3":
                await self.retry_failed_classifications()
            elif choice == "4":
                return
            else:
                await self.classification_settings()

    async def run_advertio_delivery(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Advertio Delivery")

        if not config.ADVERTIO_INGEST_ENABLED:
            self.show_message("Advertio ingestion is disabled in configuration.", Fore.YELLOW)
            self.show_message("Set TELCLAW_ADVERTIO_INGEST_ENABLED=true to enable it.", Fore.YELLOW)
            await self.pause()
            return

        if self.advertio_service is None:
            self.show_message("Advertio is enabled but the ingest key/configuration is missing.", Fore.RED)
            self.show_message("Configure TELCLAW_ADVERTIO_INGEST_KEY and restart Telclaw.", Fore.YELLOW)
            await self.pause()
            return

        try:
            preview = self.advertio_service.repository.get_advertio_pending(limit=100, channel_username=None)
            eligible = len(preview)
            if eligible == 0:
                self.show_message("No eligible housing listings are waiting for Advertio.", Fore.YELLOW)
                self.show_message("No new crawl or AI processing is required for this menu.", Fore.CYAN)
                await self.pause()
                return

            self.show_message(
                f"Eligible listings found: {eligible} (showing up to the first 100).",
                Fore.CYAN,
            )
            self.show_message(
                "Eligible = AI processed + housing data exists + Advertio status is waiting/retry.",
                Fore.CYAN,
            )
            limit_text = await self.prompt_text(
                "How many listings should be sent",
                default=str(min(eligible, 100)),
                allow_empty=False,
            )
            try:
                limit = int(limit_text)
                if limit <= 0:
                    raise ValueError
            except ValueError:
                self.show_message("Number of listings must be a positive integer.", Fore.RED)
                await self.pause()
                return

            confirm = await self.prompt_choice(
                f"Send up to {limit} existing listing(s) to Advertio? [y/n]: ",
                {"y", "n"},
            )
            if confirm == "n":
                self.show_message("Advertio delivery cancelled.", Fore.YELLOW)
                await self.pause()
                return

            self.show_message(
                "Starting Advertio delivery. No Telegram crawl and no AI extraction will run.",
                Fore.CYAN,
            )
            result = self.advertio_service.deliver_pending(limit=limit, progress=True)
            color = Fore.GREEN if result["failed"] == 0 else Fore.YELLOW
            self.show_message(
                f"Completed. Found: {result['found']} | Sent: {result['sent']} | "
                f"Already existed: {result['already_existed']} | Failed: {result['failed']}",
                color,
            )
        except Exception as exc:
            self.show_message(f"Advertio delivery failed: {exc}", Fore.RED)
        await self.pause()

    async def run_groq_connection_test(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Groq Connection Test")
        try:
            success = test_groq_connection()
            self.show_message(
                "Groq minimal connection test succeeded."
                if success
                else "Groq minimal connection test failed. See diagnostic output above.",
                Fore.GREEN if success else Fore.RED,
            )
        except Exception as exc:
            self.show_message(f"Groq connection test failed: {exc}", Fore.RED)
        await self.pause()

    async def run(self):
        while True:
            self.clear_screen()
            self.show_banner()
            self.show_section_header("Main Menu")
            print(f"{Fore.GREEN}│  1. ▶️ Start scheduled crawler")
            print(f"{Fore.GREEN}│  2. 🧹 Process information queue")
            print(f"{Fore.GREEN}│  3. 🤖 Process AI queue")
            print(f"{Fore.GREEN}│  4. 🏷️ AI Category Classification")
            print(f"{Fore.GREEN}│  5. 📤 Send eligible ads to Advertio")
            print(f"{Fore.GREEN}│  6. 🔬 Test Groq connection")
            print(f"{Fore.GREEN}│  7. ⚙️ Change settings")
            print(f"{Fore.GREEN}│  8. 📋 Manage channels")
            print(f"{Fore.GREEN}│  9. 👤 Switch / add account")
            print(f"{Fore.GREEN}│  10. 🚪 Exit")
            self.show_section_footer()

            choice = await self.prompt_choice(
                "\nChoose an option [1-10]: ",
                {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"},
            )
            if choice == "1":
                await self.start_crawler_flow()
            elif choice == "2":
                await self.run_processing_queue()
            elif choice == "3":
                await self.run_ai_queue()
            elif choice == "4":
                await self.classification_menu()
            elif choice == "5":
                await self.run_advertio_delivery()
            elif choice == "6":
                await self.run_groq_connection_test()
            elif choice == "7":
                await self.change_settings()
            elif choice == "8":
                await self.manage_channels()
            elif choice == "9":
                await self.account_menu()
            else:
                self.crawler.stop_all()
                await self.accounts.disconnect(self.client)
                self.client = None
                break
