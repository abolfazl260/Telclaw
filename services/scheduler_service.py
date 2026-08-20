"""Independent scheduling service for collection, processing, and AI queues."""

import asyncio

import config
from ai.ai_service import AIProcessingService
from collection.crawler import CRAWL_MODE_ALL
from services.crawl_job_service import CrawlJobService
from services.processing_service import ProcessingService


class SchedulerService:
    """Run three independent workers while preserving queue order in SQLite."""

    def __init__(self, crawl_job_service=None, processing_service=None, ai_service=None):
        self.crawl_job = crawl_job_service or CrawlJobService()
        self.processing = processing_service or ProcessingService()
        self.ai = ai_service or AIProcessingService()
        self._tasks = {}
        self._worker_tasks = {}

    @staticmethod
    def _task_key(client, channel_username, from_date, to_date, crawl_mode):
        session = getattr(client, "session", None)
        session_name = getattr(session, "filename", None) or str(session)
        return (
            f"{session_name}:{channel_username.lower().lstrip('@')}:"
            f"{from_date}:{to_date}:{crawl_mode}"
        )

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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[COLLECTION] Job failed for {channel_username}: {exc}")

            print(
                f"[COLLECTION] Next run for {channel_username} "
                f"in {interval_minutes:g} minute(s)."
            )
            await asyncio.sleep(interval_minutes * 60)

    async def _run_processing_worker(self, interval_minutes):
        while True:
            try:
                stats = self.processing.process_pending_with_stats()
                print(
                    "[PROCESSING QUEUE] "
                    f"found={stats['found']} processed={stats['processed']} failed={stats['failed']}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[PROCESSING QUEUE] Worker failed: {exc}")
            await asyncio.sleep(interval_minutes * 60)

    async def _run_ai_worker(self, interval_minutes):
        while True:
            try:
                stats = self.ai.process_pending_with_stats()
                print(
                    "[AI QUEUE] "
                    f"found={stats['found']} processed={stats['processed']} "
                    f"failed={stats['failed']} skipped={stats.get('skipped', False)}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[AI QUEUE] Worker failed: {exc}")
            await asyncio.sleep(interval_minutes * 60)

    def _ensure_workers(self):
        worker_specs = {
            "processing": (
                self._run_processing_worker,
                float(getattr(config, "PROCESSING_INTERVAL_MINUTES", 1)),
            ),
            "ai": (
                self._run_ai_worker,
                float(getattr(config, "AI_INTERVAL_MINUTES", 1)),
            ),
        }
        for name, (worker, interval) in worker_specs.items():
            if interval <= 0:
                raise ValueError(f"{name} worker interval must be greater than zero")
            existing = self._worker_tasks.get(name)
            if existing and not existing.done():
                continue
            self._worker_tasks[name] = asyncio.create_task(worker(interval))

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

        self._ensure_workers()

        interval = float(
            interval_minutes
            if interval_minutes is not None
            else getattr(config, "CRAWL_INTERVAL_MINUTES", 5)
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
        jobs = {k: v for k, v in self._tasks.items() if not v.done()}
        jobs.update(
            {
                f"worker:{k}": v
                for k, v in self._worker_tasks.items()
                if not v.done()
            }
        )
        return jobs

    def stop_all(self):
        for task in list(self._tasks.values()) + list(self._worker_tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._worker_tasks.clear()
