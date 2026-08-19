"""Application-level scheduling service."""

import asyncio

import config
from collection.crawler import CRAWL_MODE_ALL
from services.crawl_job_service import CrawlJobService


class SchedulerService:
    """Schedule crawl jobs; collection and processing stay replaceable."""

    def __init__(self, crawl_job_service=None):
        self.crawl_job = crawl_job_service or CrawlJobService()
        self._tasks = {}

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
                print(f"[SCHEDULER] Job failed for {channel_username}: {exc}")

            print(
                f"[SCHEDULER] Next run for {channel_username} "
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
        return {k: v for k, v in self._tasks.items() if not v.done()}

    def stop_all(self):
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
