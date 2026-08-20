"""Scheduling service for ordered crawl -> processing -> AI cycles."""

import asyncio

from colorama import Fore

from ai.ai_service import AIProcessingService
from collection.crawler import CRAWL_MODE_ALL
from services.crawl_job_service import CrawlJobService
from services.processing_service import ProcessingService


class SchedulerService:
    """Run each channel cycle in the explicit order: crawl, process, then AI."""

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
        """Process and AI are deliberately started only after crawl completion."""
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
    ):
        while True:
            try:
                await self.crawl_job.run_channel(
                    client,
                    channel_username,
                    from_date,
                    to_date,
                    crawl_mode=crawl_mode,
                )
                # IMPORTANT: no processing/AI can start until the crawl call above
                # has returned. This prevents partially crawled data from reaching
                # the downstream queues.
                await self._run_post_crawl_pipeline(channel_username)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[COLLECTION] Pipeline failed for {channel_username}: {exc}")

            print(
                f"[COLLECTION] Next complete crawl cycle for {channel_username} "
                f"in {interval_minutes:g} minute(s)."
            )
            await asyncio.sleep(interval_minutes * 60)

    def schedule_channel(
        self,
        client,
        channel_username,
        from_date,
        to_date,
        interval_minutes=None,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        if from_date > to_date:
            raise ValueError("Start date cannot be later than end date")

        interval = float(
            interval_minutes
            if interval_minutes is not None
            else 5
        )
        if interval <= 0:
            raise ValueError("Crawler interval must be greater than zero")

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
