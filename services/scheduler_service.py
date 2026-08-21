"""Scheduling service for continuous crawl -> processing -> AI cycles."""

import asyncio
import time
from datetime import date

from colorama import Fore

import config
from ai.ai_service import AIProcessingService
from collection.crawler import CRAWL_MODE_ALL
from services.crawl_job_service import CrawlJobService
from services.processing_service import ProcessingService


class SchedulerService:
    """Run continuous channel cycles in the order crawl -> process -> AI.

    The configured end date is used only for the first historical crawl. Every
    later cycle switches its date window to the current day, so newly published
    Telegram messages are checked forever instead of being trapped inside the
    original date range.
    """

    def __init__(self, crawl_job_service=None, processing_service=None, ai_service=None):
        self.crawl_job = crawl_job_service or CrawlJobService()
        self.processing = processing_service or ProcessingService()
        self.ai = ai_service or AIProcessingService()
        self._tasks = {}
        self._pipeline_lock = asyncio.Lock()

    @staticmethod
    def _task_key(client, channel_username, from_date, to_date, crawl_mode):
        session = getattr(client, "session", None)
        session_name = getattr(session, "filename", None) or str(session)
        return (
            f"{session_name}:{channel_username.lower().lstrip('@')}:"
            f"{from_date}:{to_date}:{crawl_mode}"
        )

    async def _run_post_crawl_pipeline(self, channel_username):
        """Process and AI only after the channel crawl has completely finished."""
        async with self._pipeline_lock:
            print(f"\n{Fore.CYAN}{'=' * 60}")
            print(f"{Fore.GREEN}✅ CRAWL COMPLETED: @{channel_username}")
            print(f"{Fore.CYAN}{'=' * 60}")

            print(f"{Fore.CYAN}▸ Starting normal information processing...")
            processing_stats = await asyncio.to_thread(
                self.processing.process_pending_with_stats,
            )
            print(
                f"{Fore.GREEN}✅ Normal processing completed | "
                f"Found: {processing_stats['found']} | "
                f"Processed: {processing_stats['processed']} | "
                f"Failed: {processing_stats['failed']}"
            )

            print(f"\n{Fore.CYAN}▸ Starting AI processing...")
            ai_stats = await asyncio.to_thread(self.ai.process_pending_with_stats)
            if ai_stats.get("disabled"):
                print(f"{Fore.YELLOW}⚠️ AI extraction is disabled in configuration.")
            else:
                print(
                    f"{Fore.GREEN}✅ AI processing completed | "
                    f"Found: {ai_stats['found']} | "
                    f"Processed: {ai_stats['processed']} | "
                    f"Skipped: {ai_stats['skipped']} | "
                    f"Failed: {ai_stats['failed']}"
                )
                if ai_stats.get("stopped"):
                    print(f"{Fore.YELLOW}⚠️ AI queue stopped; remaining records stay pending.")

            return processing_stats, ai_stats

    async def _run_forever(
        self,
        client,
        channel_username,
        interval_minutes,
        from_date,
        to_date,
        crawl_mode,
        start_delay_minutes=0,
    ):
        if start_delay_minutes > 0:
            print(
                f"[SCHEDULER] @{channel_username} starts in "
                f"{start_delay_minutes:g} minute(s) to preserve channel spacing."
            )
            await asyncio.sleep(start_delay_minutes * 60)

        first_cycle = True
        next_run_at = time.monotonic()
        while True:
            cycle_started_at = time.monotonic()
            try:
                if first_cycle:
                    cycle_from_date = from_date
                    cycle_to_date = to_date
                    print(
                        f"[SCHEDULER] @{channel_username} initial historical crawl: "
                        f"{cycle_from_date} -> {cycle_to_date}"
                    )
                else:
                    # The configured end date is NOT reused. This is a live watcher.
                    # Each cycle checks the current day for newly published messages.
                    today = date.today()
                    cycle_from_date = max(from_date, today)
                    cycle_to_date = today
                    print(
                        f"[SCHEDULER] @{channel_username} live crawl: "
                        f"{cycle_from_date} -> {cycle_to_date}"
                    )

                await self.crawl_job.run_channel(
                    client,
                    channel_username,
                    cycle_from_date,
                    cycle_to_date,
                    crawl_mode=crawl_mode,
                )

                # Mandatory downstream pipeline: crawl -> processing -> AI.
                await self._run_post_crawl_pipeline(channel_username)
                first_cycle = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[COLLECTION] Pipeline failed for {channel_username}: {exc}")
                first_cycle = False

            # Schedule the next START at a fixed interval rather than sleeping
            # interval_minutes after the pipeline. This preserves the configured
            # spacing between channels/groups even when processing takes time.
            next_run_at += interval_minutes * 60
            sleep_seconds = max(0, next_run_at - time.monotonic())
            print(
                f"[COLLECTION] Next check for {channel_username} "
                f"in {sleep_seconds / 60:g} minute(s)."
            )
            await asyncio.sleep(sleep_seconds)

    def schedule_channel(
        self,
        client,
        channel_username,
        from_date,
        to_date,
        interval_minutes=None,
        start_delay_minutes=None,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        if from_date > to_date:
            raise ValueError("Start date cannot be later than end date")

        interval = float(
            interval_minutes
            if interval_minutes is not None
            else config.CRAWL_INTERVAL_MINUTES
        )
        if interval <= 0:
            raise ValueError("Crawler interval must be greater than zero")

        stagger = float(
            start_delay_minutes
            if start_delay_minutes is not None
            else 0
        )
        if stagger < 0:
            raise ValueError("Channel start delay cannot be negative")

        key = self._task_key(
            client,
            channel_username,
            from_date,
            to_date,
            crawl_mode,
        )
        existing = self._tasks.get(key)
        if existing and not existing.done():
            return existing

        task = asyncio.create_task(
            self._run_forever(
                client,
                channel_username,
                interval,
                from_date,
                to_date,
                crawl_mode,
                start_delay_minutes=stagger,
            )
        )
        self._tasks[key] = task
        return task

    def active_jobs(self):
        return {k: v for k, v in self._tasks.items() if not v.done()}

    def stop_all(self):
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
