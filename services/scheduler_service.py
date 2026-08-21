"""Scheduling service for continuous crawl -> processing -> AI cycles."""

import asyncio
import time
from datetime import date

from colorama import Fore

import config
from ai.ai_service import AIProcessingService
from collection.crawler import CRAWL_MODE_ALL
from collection.media_downloader import download_photo_for_record
from services.crawl_job_service import CrawlJobService
from services.processing_service import ProcessingService
from monitoring.telegram_monitor import get_telegram_monitor


class SchedulerService:
    """Run continuous channel cycles in the order crawl -> process -> AI."""

    def __init__(self, crawl_job_service=None, processing_service=None, ai_service=None):
        self.crawl_job = crawl_job_service or CrawlJobService()
        self.processing = processing_service or ProcessingService()
        self.ai = ai_service or AIProcessingService()
        self.monitor = get_telegram_monitor()
        self._tasks = {}
        self._pipeline_lock = asyncio.Lock()

    @staticmethod
    def _task_key(client, channel_username, from_date, to_date, crawl_mode):
        session = getattr(client, "session", None)
        session_name = getattr(session, "filename", None) or str(session)
        return f"{session_name}:{channel_username.lower().lstrip('@')}:{from_date}:{to_date}:{crawl_mode}"

    @staticmethod
    def _make_sync_media_downloader(client):
        """Bridge the async Telethon downloader into the AI worker thread."""
        loop = asyncio.get_running_loop()

        def download(record):
            future = asyncio.run_coroutine_threadsafe(
                download_photo_for_record(client, record), loop
            )
            return future.result()

        return download

    async def _run_post_crawl_pipeline(self, client, channel_username, crawl_result=None):
        async with self._pipeline_lock:
            print(f"\n{Fore.CYAN}{'=' * 60}")
            print(f"{Fore.GREEN}✅ CRAWL COMPLETED: @{channel_username}")
            print(f"{Fore.CYAN}{'=' * 60}")

            crawl_stats = self._normalize_crawl_stats(crawl_result)
            crawl_stats.setdefault("channel", f"@{channel_username}")
            await self.monitor.report("crawl", crawl_stats)

            print(f"{Fore.CYAN}▸ Starting normal information processing...")
            processing_stats = await asyncio.to_thread(self.processing.process_pending_with_stats)
            print(f"{Fore.GREEN}✅ Normal processing completed | Found: {processing_stats['found']} | Processed: {processing_stats['processed']} | Failed: {processing_stats['failed']}")
            await self.monitor.report("processing", processing_stats)

            # AI runs in a worker thread. The existing Telethon client remains
            # owned by the main event loop, so provide a safe bridge to the
            # existing media download operation. It is called only after AI
            # accepts a housing advertisement and immediately before Advertio.
            self.ai.set_media_downloader(self._make_sync_media_downloader(client))

            print(f"\n{Fore.CYAN}▸ Starting AI processing...")
            ai_stats = await asyncio.to_thread(self.ai.process_pending_with_stats)
            if ai_stats.get("disabled"):
                print(f"{Fore.YELLOW}⚠️ AI extraction is disabled in configuration.")
            else:
                print(f"{Fore.GREEN}✅ AI processing completed | Found: {ai_stats['found']} | Processed: {ai_stats['processed']} | Skipped: {ai_stats['skipped']} | Failed: {ai_stats['failed']}")
                if ai_stats.get("stopped"):
                    print(f"{Fore.YELLOW}⚠️ AI queue stopped; remaining records stay pending.")
            await self.monitor.report("ai", {k: v for k, v in ai_stats.items() if k != "disabled"})

            advertio_stats = ai_stats.get("advertio")
            if advertio_stats is not None:
                await self.monitor.report("advertio", advertio_stats)

            return processing_stats, ai_stats

    @staticmethod
    def _normalize_crawl_stats(result):
        if isinstance(result, dict):
            return dict(result)
        stats = {}
        for name in ("saved", "media_saved", "media_failed", "filtered", "bot_skipped", "no_username", "skipped", "from_date", "to_date"):
            if hasattr(result, name):
                stats[name] = getattr(result, name)
        return stats or {"status": "completed"}

    async def _run_forever(self, client, channel_username, interval_minutes, from_date, to_date, crawl_mode, start_delay_minutes=0):
        if start_delay_minutes > 0:
            print(f"[SCHEDULER] @{channel_username} starts in {start_delay_minutes:g} minute(s) to preserve channel spacing.")
            await asyncio.sleep(start_delay_minutes * 60)

        first_cycle = True
        next_run_at = time.monotonic()
        while True:
            try:
                if first_cycle:
                    cycle_from_date, cycle_to_date = from_date, to_date
                    print(f"[SCHEDULER] @{channel_username} initial historical crawl: {cycle_from_date} -> {cycle_to_date}")
                else:
                    today = date.today()
                    cycle_from_date, cycle_to_date = max(from_date, today), today
                    print(f"[SCHEDULER] @{channel_username} live crawl: {cycle_from_date} -> {cycle_to_date}")

                crawl_result = await self.crawl_job.run_channel(client, channel_username, cycle_from_date, cycle_to_date, crawl_mode=crawl_mode)
                await self._run_post_crawl_pipeline(client, channel_username, crawl_result)
                first_cycle = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[COLLECTION] Pipeline failed for {channel_username}: {exc}")
                await self.monitor.error("ERROR", "services.scheduler_service", f"Pipeline failed for @{channel_username}: {exc}")
                first_cycle = False

            next_run_at += interval_minutes * 60
            sleep_seconds = max(0, next_run_at - time.monotonic())
            print(f"[COLLECTION] Next check for {channel_username} in {sleep_seconds / 60:g} minute(s).")
            await asyncio.sleep(sleep_seconds)

    def schedule_channel(self, client, channel_username, from_date, to_date, interval_minutes=None, start_delay_minutes=None, crawl_mode=CRAWL_MODE_ALL):
        if from_date > to_date:
            raise ValueError("Start date cannot be later than end date")
        interval = float(interval_minutes if interval_minutes is not None else config.CRAWL_INTERVAL_MINUTES)
        if interval <= 0:
            raise ValueError("Crawler interval must be greater than zero")
        stagger = float(start_delay_minutes if start_delay_minutes is not None else 0)
        if stagger < 0:
            raise ValueError("Channel start delay cannot be negative")
        key = self._task_key(client, channel_username, from_date, to_date, crawl_mode)
        existing = self._tasks.get(key)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self._run_forever(client, channel_username, interval, from_date, to_date, crawl_mode, start_delay_minutes=stagger))
        self._tasks[key] = task
        return task

    def active_jobs(self):
        return {k: v for k, v in self._tasks.items() if not v.done()}

    def stop_all(self):
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
