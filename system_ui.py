"""System menu extensions for independently running database-backed queues."""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from colorama import Fore

from ai.ai_service import AIProcessingService
from ai.classification_service import CategoryClassificationService
from ai.groq_connection_test import test_groq_connection
from collection.media_downloader import download_photos_for_record
from delivery.advertio_service import AdvertioDeliveryService, AdvertioMappingError
from delivery.telegram_transfer import (
    get_ready_transfer_ads,
    get_transfer_queue_status,
    get_unsent_transfer_ads,
    get_unsent_transfer_ads_count,
    send_transfer_ads,
)
from services.processing_service import ProcessingService
import config
from storage import database
from ui import ConsoleUI
from terminal_input import ConsoleBack, read_line


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
                download_photos_for_record(self.client, record), loop
            )
            return future.result()

        return download

    async def _run_with_q_stop(self, operation):
        """Run an operation with Q + Enter as a cooperative stop request.

        The shared stdin reader prevents a completed operation from leaving
        a blocked input thread that consumes the next menu selection.
        """
        stop_event = asyncio.Event()

        async def wait_for_q():
            self.show_message("Press Q + Enter to stop after the current item.", Fore.YELLOW)
            while not stop_event.is_set():
                try:
                    value = await read_line()
                except EOFError:
                    stop_event.set()
                    return
                if value.strip().casefold() == "q":
                    stop_event.set()
                    self.show_message(
                        "Stop requested. The current item will finish, then the queue will stop.",
                        Fore.YELLOW,
                    )
                    return

        stop_task = asyncio.create_task(wait_for_q())
        try:
            return await operation(stop_event.is_set)
        finally:
            stop_event.set()
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass

    async def run_processing_queue(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Information Processing Queue")
        self.show_message("Checking the processing queue in the database...", Fore.CYAN)
        try:
            async with self.crawler.scheduler._pipeline_lock:
                result = await self._run_with_q_stop(
                    lambda should_stop: asyncio.to_thread(
                        self.processing_service.process_pending_with_stats,
                        should_stop=should_stop,
                    )
                )
            status = "Stopped" if result.get("stopped") else "Completed"
            self.show_message(
                f"{status}. Found: {result['found']} | "
                f"Processed: {result['processed']} | Failed: {result['failed']}",
                Fore.GREEN if result["failed"] == 0 and not result.get("stopped") else Fore.YELLOW,
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
            async with self.crawler.scheduler._pipeline_lock:
                result = await self._run_with_q_stop(
                    lambda should_stop: asyncio.to_thread(
                        self.ai_service.process_pending_with_stats,
                        should_stop=should_stop,
                    )
                )
            if result.get("disabled"):
                self.show_message("AI extraction is disabled in configuration.", Fore.YELLOW)
            else:
                remaining = result.get("remaining", 0)
                if result.get("stopped"):
                    status = f"Stopped. Remaining pending: {remaining}"
                else:
                    status = f"Completed. Remaining pending: {remaining}"
                self.show_message(
                    f"{status} | Found: {result['found']} | "
                    f"Processed: {result['processed']} | Failed: {result['failed']} | "
                    f"Skipped: {result['skipped']}",
                    Fore.GREEN if result["failed"] == 0 and not result.get("stopped") else Fore.YELLOW,
                )
        except ConsoleBack:
            raise
        except Exception as exc:
            self.show_message(f"AI queue failed: {exc}", Fore.RED)
        await self.pause()

    def show_classification_queue_summary(self):
        """Render a queue snapshot using the exact eligibility query used by the worker."""
        status = self.classification_service.repository.get_classification_queue_status()
        pending_records = self.classification_service.repository.get_classification_pending(limit=100000)
        status["pending"] = len(pending_records)
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

        interval_value = await self.prompt_text(
            "Batch interval in seconds",
            default="20",
            allow_empty=False,
        )
        try:
            batch_interval = float(interval_value)
            if batch_interval < 0:
                raise ValueError
        except ValueError:
            self.show_message("Batch interval must be zero or a positive number.", Fore.RED)
            await self.pause()
            return

        self.show_message("Sending ready messages to AI for category classification...", Fore.CYAN)
        total_found = total_processed = total_failed = total_skipped = 0
        batch_number = 0
        stopped = False

        async def process_batches(should_stop):
            nonlocal total_found, total_processed, total_failed, total_skipped, batch_number, stopped
            while True:
                if should_stop():
                    stopped = True
                    break

                batch_number += 1
                result = await asyncio.to_thread(
                    self.classification_service.process_pending_with_stats,
                    batch_size,
                    should_stop=should_stop,
                )

                if result.get("disabled"):
                    self.show_message("AI category classification is disabled in configuration.", Fore.YELLOW)
                    self.show_message("Set TELCLAW_AI_CLASSIFICATION_ENABLED=true to enable it.", Fore.YELLOW)
                    break

                total_found += result["found"]
                total_processed += result["processed"]
                total_failed += result["failed"]
                total_skipped += result["skipped"]

                if result["found"] == 0 or result.get("stopped"):
                    stopped = stopped or result.get("stopped", False)
                    break

                remaining = len(
                    await asyncio.to_thread(
                        self.classification_service.repository.get_classification_pending,
                        limit=100000,
                    )
                )
                if remaining == 0:
                    break

                if should_stop():
                    stopped = True
                    break

                self.show_message(
                    f"Batch {batch_number} completed. Found: {result['found']} | "
                    f"Classified: {result['processed']} | Failed: {result['failed']} | "
                    f"Skipped: {result['skipped']}",
                    Fore.GREEN if result["failed"] == 0 else Fore.YELLOW,
                )

                if batch_interval > 0:
                    self.show_message(
                        f"Waiting {batch_interval:g} seconds before the next batch...",
                        Fore.CYAN,
                    )
                    remaining_wait = batch_interval
                    while remaining_wait > 0:
                        if should_stop():
                            stopped = True
                            break
                        seconds_left = int(remaining_wait + 0.999999)
                        print(f"[AI CLASSIFICATION] Next batch in {seconds_left}s")
                        await asyncio.sleep(min(1.0, remaining_wait))
                        remaining_wait -= 1.0
                    if stopped:
                        break

        try:
            async with self.crawler.scheduler._pipeline_lock:
                await self._run_with_q_stop(process_batches)
            color = Fore.GREEN if total_failed == 0 and not stopped else Fore.YELLOW
            status = "Stopped" if stopped else "Completed"
            self.show_message(
                f"{status}. Found: {total_found} | Classified: {total_processed} | "
                f"Failed: {total_failed} | Skipped: {total_skipped}",
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
        print(f"{Fore.GREEN}│  1. ⬅ Back")
        self.show_section_footer()
        self.show_message("Set TELCLAW_AI_CLASSIFICATION_BATCH_SIZE in configuration to change the default.", Fore.CYAN)
        await self._prompt_back()

    async def classification_menu(self):
        """Open the manual controls for the independent AI classification queue."""
        while True:
            self.clear_screen()
            self.show_banner()
            self.show_classification_queue_summary()
            print(f"{Fore.GREEN}│  1. Start Classification")
            print(f"{Fore.GREEN}│  2. View Queue Status")
            print(f"{Fore.GREEN}│  3. Retry Failed")
            print(f"{Fore.GREEN}│  4. Classification Settings")
            print(f"{Fore.GREEN}│  5. ⬅ Back")
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
                await self.classification_settings()
            else:
                return

    @staticmethod
    def _transfer_status_label(status):
        return {
            "sent": "SENT",
            "failed": "FAILED",
            "waiting": "NOT SENT",
        }.get(status, "NOT SENT")

    @staticmethod
    def _format_transfer_record(record, index):
        origin = record.get("origin_city") or record.get("origin_country") or "?"
        destination = record.get("destination_city") or record.get("destination_country") or "?"
        title = record.get("title") or f"{origin} → {destination}"
        price = record.get("price")
        currency = record.get("currency") or ""
        price_text = f"{price} {currency}".strip() if price is not None else "-"
        departure = " ".join(
            value for value in (record.get("departure_date"), record.get("departure_time")) if value
        ) or "-"
        return (
            f"{Fore.GREEN}│  {index:>2}. {title}\n"
            f"{Fore.GREEN}│      Route: {origin} → {destination} | "
            f"Departure: {departure} | Price: {price_text}\n"
            f"{Fore.GREEN}│      Contact: {record.get('contact') or '-'} | "
            f"Status: {SystemConsoleUI._transfer_status_label(record.get('delivery_status'))} | "
            f"Source: @{record.get('channel_username') or 'unknown'}"
        )

    async def view_transfer_ads(self):
        """Browse unsent transfer ads 20 at a time; successfully sent ads are hidden."""
        page_size = 20
        offset = 0

        while True:
            self.clear_screen()
            self.show_banner()
            try:
                total = get_unsent_transfer_ads_count()
                records = get_unsent_transfer_ads(limit=page_size, offset=offset)
            except Exception as exc:
                self.show_section_header("Transfer Ads")
                self.show_message(f"Unable to read transfer ads: {exc}", Fore.RED)
                self.show_section_footer()
                await self.pause()
                return

            if total == 0:
                self.show_section_header("Transfer Ads")
                self.show_message("No unsent transfer ads exist in the database.", Fore.YELLOW)
                self.show_section_footer()
                await self.pause()
                return

            current_page = (offset // page_size) + 1
            total_pages = (total + page_size - 1) // page_size
            self.show_section_header(
                f"Transfer Ads — Unsent {offset + 1}-{min(offset + len(records), total)} of {total}"
            )
            for index, record in enumerate(records, start=offset + 1):
                print(self._format_transfer_record(record, index))

            self.show_section_footer()
            options = {"b"}
            if offset + page_size < total:
                options.add("n")
            if offset > 0:
                options.add("p")

            navigation = []
            if "n" in options:
                navigation.append("N = Next 20")
            if "p" in options:
                navigation.append("P = Previous 20")
            navigation.append("B = Back")
            print(f"{Fore.CYAN}│  Page {current_page}/{total_pages} | " + " | ".join(navigation))
            self.show_section_footer()

            choice = await self.prompt_choice(
                "\nChoose an option [N/P/B]: ",
                options,
            )
            if choice == "n":
                offset += page_size
            elif choice == "p":
                offset = max(0, offset - page_size)
            else:
                return

    async def send_transfer_ads(self):
        """Send unsent transfer ads, including previous failed attempts, to a selected channel."""
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Send Transfer Ads")
        if config.BACKOFFICE_ENABLED:
            self.show_message("Rule-based publishing is active. Manage destinations in the back office.", Fore.YELLOW)
            await self.pause()
            return
        try:
            status = get_transfer_queue_status()
            available = status["waiting"] + status["failed"]
            if available == 0:
                self.show_message("No transfer ads are currently available to send.", Fore.YELLOW)
                await self.pause()
                return

            self.show_message(
                f"Not sent: {status['waiting']} | Failed: {status['failed']} | Already sent: {status['sent']}",
                Fore.CYAN,
            )
            target_channel = await self.prompt_text(
                "Target Telegram channel (e.g. @my_channel)",
                allow_empty=False,
            )
            if not target_channel.startswith("@"):
                target_channel = f"@{target_channel}"

            count_text = await self.prompt_text(
                "How many ads should be sent",
                default=str(min(available, 20)),
                allow_empty=False,
            )
            try:
                limit = int(count_text)
                if limit <= 0:
                    raise ValueError
            except ValueError:
                self.show_message("Number of ads must be a positive integer.", Fore.RED)
                await self.pause()
                return

            confirm = await self.prompt_choice(
                f"Send up to {limit} transfer ad(s) to {target_channel}? [y/n]: ",
                {"y", "n"},
            )
            if confirm == "n":
                self.show_message("Transfer delivery cancelled.", Fore.YELLOW)
                await self.pause()
                return

            client = await self.connect_client()
            if client is None:
                await self.pause()
                return

            result = await self._run_with_q_stop(
                lambda should_stop: send_transfer_ads(
                    client, target_channel, limit=limit, should_stop=should_stop
                )
            )
            stopped = result.get("stopped", False)
            color = Fore.GREEN if result["failed"] == 0 and not stopped else Fore.YELLOW
            self.show_message(
                f"{'Stopped' if stopped else 'Completed'}. Found: {result['found']} | "
                f"Sent: {result['sent']} | Failed: {result['failed']}",
                color,
            )
        except ConsoleBack:
            raise
        except Exception as exc:
            self.show_message(f"Transfer delivery failed: {exc}", Fore.RED)
        await self.pause()

    async def transfer_ads_menu(self):
        """Manage transfer-list delivery without crawling or AI processing."""
        while True:
            self.clear_screen()
            self.show_banner()
            self.show_section_header("Transfer Ads")
            try:
                status = get_transfer_queue_status()
                print(f"{Fore.GREEN}│  📦 Total transfer ads: {status['total']}")
                print(f"{Fore.GREEN}│  ✅ Already sent:      {status['sent']}")
                print(f"{Fore.GREEN}│  📤 Not sent:          {status['waiting']}")
                print(f"{Fore.GREEN}│  ❌ Failed / not sent: {status['failed']}")
            except Exception as exc:
                self.show_message(f"Unable to read transfer queue: {exc}", Fore.RED)
                await self.pause()
                return

            self.show_section_footer()
            print(f"{Fore.GREEN}│  1. 📤 Send in channel")
            print(f"{Fore.GREEN}│  2. 📋 View unsent transfer ads (20 at a time)")
            print(f"{Fore.GREEN}│  3. 🔄 Refresh status")
            print(f"{Fore.GREEN}│  4. ⬅ Back")
            self.show_section_footer()

            choice = await self.prompt_choice("\nChoose an option [1-4]: ", {"1", "2", "3", "4"})
            if choice == "1":
                await self.send_transfer_ads()
            elif choice == "2":
                await self.view_transfer_ads()
            elif choice == "3":
                continue
            else:
                return

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

            media_downloader = None
            selected = preview[:limit]
            if any(record.get("media_type") == "photo" for record in selected):
                client = await self.connect_client()
                if client is None:
                    self.show_message(
                        "Advertio delivery needs a connected Telegram account to prepare listing photos.",
                        Fore.RED,
                    )
                    await self.pause()
                    return
                media_downloader = self._make_sync_media_downloader()

            self.show_message(
                "Starting Advertio delivery. No Telegram crawl and no AI extraction will run.",
                Fore.CYAN,
            )
            async with self.crawler.scheduler._pipeline_lock:
                result = await self._run_with_q_stop(
                    lambda should_stop: asyncio.to_thread(
                        self.advertio_service.deliver_pending,
                        limit=limit,
                        progress=True,
                        media_downloader=media_downloader,
                        should_stop=should_stop,
                    )
                )
            stopped = result.get("stopped", False)
            color = Fore.GREEN if result["failed"] == 0 and not stopped else Fore.YELLOW
            self.show_message(
                f"{'Stopped' if stopped else 'Completed'}. Found: {result['found']} | "
                f"Sent: {result['sent']} | Already existed: {result['already_existed']} | "
                f"Failed: {result['failed']}",
                color,
            )
        except ConsoleBack:
            raise
        except Exception as exc:
            self.show_message(f"Advertio delivery failed: {exc}", Fore.RED)
        await self.pause()

    async def run_groq_connection_test(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Groq Connection Test")
        try:
            async def run_check(should_stop):
                success = await asyncio.to_thread(test_groq_connection)
                return success, should_stop()

            success, stopped = await self._run_with_q_stop(run_check)
            if stopped:
                self.show_message(
                    "Stop requested. The in-flight connection test finished; returning to the menu.",
                    Fore.YELLOW,
                )
            else:
                self.show_message(
                    "Groq minimal connection test succeeded."
                    if success
                    else "Groq minimal connection test failed. See diagnostic output above.",
                    Fore.GREEN if success else Fore.RED,
                )
        except Exception as exc:
            self.show_message(f"Groq connection test failed: {exc}", Fore.RED)
        await self.pause()

    async def show_system_health(self):
        """Display the same canonical health snapshot as Telegram /health."""
        self.clear_screen()
        self.show_banner()
        self.show_section_header("System Health")
        try:
            health = database.get_pipeline_health()
            labels = (
                ("Crawler activity", "crawler"),
                ("Processing", "processing"),
                ("Classification", "classification"),
                ("AI extraction", "ai"),
                ("Advertio", "advertio"),
                ("Database", "database"),
            )
            for title, key in labels:
                state = health[key]
                color = (Fore.GREEN if state == "HEALTHY" else
                         Fore.RED if state == "CRITICAL" else Fore.YELLOW)
                self.show_message(f"{title}: {state}", color)

            def local_time(value):
                if not value:
                    return "Not recorded"
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S Tehran")

            for title, key in (
                ("Last crawl", "last_crawl"),
                ("Last processing", "last_processing"),
                ("Last classification", "last_classification"),
                ("Last AI", "last_ai"),
                ("Last Advertio", "last_advertio"),
            ):
                self.show_message(f"{title}: {local_time(health[key])}", Fore.CYAN)
            self.show_message(f"Pipeline backlog (unique messages): {health['backlog']}", Fore.CYAN)
            self.show_message(f"Failed items (unique messages): {health['failed']}", Fore.YELLOW)
            for name, key in (("Processing", "processing"), ("Classification", "classification"), ("AI", "ai"), ("Advertio", "advertio")):
                self.show_message(
                    f"{name} eligible / failed: {health[key + '_pending']} / {health[key + '_failed']}",
                    Fore.CYAN,
                )
            if health["warning"]:
                self.show_message(f"Warning: {health['warning']}", Fore.YELLOW)
        except Exception as exc:
            self.show_message(f"Health report unavailable: {exc}", Fore.RED)
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
            print(f"{Fore.GREEN}│  6. ✈️ Transfer Ads")
            print(f"{Fore.GREEN}│  7. 🔬 Test Groq connection")
            print(f"{Fore.GREEN}│  8. ⚙️ Change settings")
            print(f"{Fore.GREEN}│  9. 📋 Manage channels")
            print(f"{Fore.GREEN}│  10. 👤 Switch / add account")
            print(f"{Fore.GREEN}│  11. 🚪 Exit")
            print(f"{Fore.GREEN}│  12. 🏥 System Health")
            print(f"{Fore.YELLOW}│  Q. 🚪 Exit and stop scheduled crawler jobs")
            self.show_section_footer()

            try:
                choice = await self.prompt_choice(
                    "\nChoose an option [1-12/Q]: ",
                    {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"},
                )
            except (ConsoleBack, EOFError):
                choice = "11"

            if choice == "11":
                self.crawler.stop_all()
                await self.accounts.disconnect(self.client)
                self.client = None
                self.client_account = None
                break

            try:
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
                    await self.transfer_ads_menu()
                elif choice == "7":
                    await self.run_groq_connection_test()
                elif choice == "8":
                    await self.change_settings()
                elif choice == "9":
                    await self.manage_channels()
                elif choice == "10":
                    await self.account_menu()
                elif choice == "12":
                    await self.show_system_health()
            except ConsoleBack:
                self.show_message("Returned to the main menu.", Fore.YELLOW)
            except EOFError:
                self.crawler.stop_all()
                await self.accounts.disconnect(self.client)
                self.client = None
                self.client_account = None
                break
