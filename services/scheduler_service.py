"""Scheduling service for continuous crawl -> processing -> AI cycles."""

import asyncio
import time
from datetime import date

from colorama import Fore

from ai.classification_service import CategoryClassificationService
from collection.crawler import CRAWL_MODE_ALL
from services.crawl_job_service import CrawlJobService
from services.processing_service import ProcessingService
from services.stage_control import get_stage_control
from monitoring.telegram_monitor import get_telegram_monitor


class SchedulerService:
    """Run continuous channel cycles in the order crawl -> process -> AI."""

    def __init__(self, crawl_job_service=None, processing_service=None, classification_service=None):
        self.crawl_job = crawl_job_service or CrawlJobService()
        self.processing = processing_service or ProcessingService()
        self.classification = classification_service or CategoryClassificationService()
        self.monitor = get_telegram_monitor()
        self.stage_control = get_stage_control()
        self._tasks = {}
        self._pipeline_lock = asyncio.Lock()

    def request_stage_skip(self, stage):
        if stage not in {"crawl", "processing", "ai", "advertio"}:
            return False
        accepted = self.stage_control.request_skip(stage)
        if accepted:
            print(f"[PIPELINE] Operator requested skip: {stage}")
        return accepted

    @staticmethod
    def _task_key(client, channel_username, from_date, to_date, crawl_mode):
        session = getattr(client, "session", None)
        session_name = getattr(session, "filename", None) or str(session)
        return f"{session_name}:{channel_username.lower().lstrip('@')}:{from_date}:{to_date}:{crawl_mode}"

    async def _run_post_crawl_pipeline(self, client, channel_username, crawl_result=None):
        async with self._pipeline_lock:
            print(f"\n{Fore.CYAN}{'=' * 60}")
            if isinstance(crawl_result, dict) and crawl_result.get("stopped"):
                print(f"{Fore.YELLOW}⏭️ CRAWL SKIPPED BY OPERATOR: @{channel_username}")
            else:
                print(f"{Fore.GREEN}✅ CRAWL COMPLETED: @{channel_username}")
            print(f"{Fore.CYAN}{'=' * 60}")

            crawl_stats = self._normalize_crawl_stats(crawl_result)
            crawl_stats.setdefault("channel", f"@{channel_username}")
            await self.monitor.report("crawl", crawl_stats)
            self.stage_control.consume_skip("crawl")

            print(f"{Fore.CYAN}▸ Starting normal information processing...")
            processing_stats = await asyncio.to_thread(
                self.processing.process_pending_with_stats,
                should_stop=lambda: self.stage_control.is_skip_requested("processing"),
            )
            if processing_stats.get("stopped"):
                print(f"{Fore.YELLOW}⚠️ Processing stage skipped by operator; remaining records stay pending.")
            else:
                print(f"{Fore.GREEN}✅ Normal processing completed | Found: {processing_stats['found']} | Processed: {processing_stats['processed']} | Failed: {processing_stats['failed']}")
            await self.monitor.report("processing", processing_stats)
            self.stage_control.consume_skip("processing")

            print(f"\n{Fore.CYAN}▸ Starting AI category classification...")
            classification_stats = await asyncio.to_thread(
                self.classification.process_pending_with_stats,
                should_stop=lambda: self.stage_control.is_skip_requested("ai"),
            )
            if classification_stats.get("disabled"):
                print(f"{Fore.YELLOW}⚠️ AI category classification is disabled in configuration.")
            elif classification_stats.get("provider_configuration_error"):
                print(
                    f"{Fore.RED}❌ AI category classification stopped: Groq rejected the configured "
                    "model or API key (HTTP 403). Check GROQ_API_KEY and TELCLAW_GROQ_MODEL."
                )
            elif classification_stats.get("stopped"):
                print(f"{Fore.YELLOW}⚠️ AI category classification skipped by operator; remaining records stay pending.")
            else:
                print(f"{Fore.GREEN}✅ AI category classification completed | Found: {classification_stats['found']} | Processed: {classification_stats['processed']} | Skipped: {classification_stats['skipped']} | Failed: {classification_stats['failed']}")
            await self.monitor.report("classification", {k: v for k, v in classification_stats.items() if k != "disabled"})
            self.stage_control.consume_skip("ai")

            return processing_stats, classification_stats

    @staticmethod
    def _normalize_crawl_stats(result):
        if isinstance(result, dict):
            return dict(result)
        stats = {}
        for name in ("saved", "media_saved", "media_failed", "filtered", "bot_skipped", "no_username", "weak_text", "skipped", "from_date", "to_date", "stopped", "status"):
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
                crawl_result = await self.crawl_job.run_channel(client, channel_username, cycle_from_date, cycle_to_date, crawl_mode=crawl_mode, should_stop=lambda: self.stage_control.is_skip_requested("crawl"))
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
