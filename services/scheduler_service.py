"""Scheduling service for continuous crawl -> processing -> AI cycles."""

import asyncio
import random
import time
from datetime import date, datetime, timezone

from colorama import Fore

import config
from ai.ai_service import AIProcessingService
from ai.classification_service import CategoryClassificationService
from collection.crawler import CRAWL_MODE_ALL
from collection.media_downloader import download_photos_for_record
from services.crawl_job_service import CrawlJobService
from services.processing_service import ProcessingService
from services.stage_control import get_stage_control
from monitoring.telegram_monitor import get_telegram_monitor


class SchedulerService:
    """Run continuous channel cycles in the order crawl -> process -> classify -> extract -> deliver."""

    def __init__(self, crawl_job_service=None, processing_service=None, classification_service=None, ai_processing_service=None):
        self.crawl_job = crawl_job_service or CrawlJobService()
        self.processing = processing_service or ProcessingService()
        self.classification = classification_service or CategoryClassificationService()
        self.ai_processing = ai_processing_service or AIProcessingService()
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

    @staticmethod
    def _make_media_downloader(client, loop):
        """Create a synchronous adapter for the loop-owned Telethon client."""
        def download_media(record):
            future = asyncio.run_coroutine_threadsafe(
                download_photos_for_record(client, record),
                loop,
            )
            return future.result()

        return download_media

    @staticmethod
    def _bind_media_downloader(ai_processing_service, client, loop):
        """Bind the current Telegram client to the synchronous AI media hook."""
        if not hasattr(ai_processing_service, "set_media_downloader"):
            return None

        downloader = SchedulerService._make_media_downloader(client, loop)
        ai_processing_service.set_media_downloader(downloader)
        return downloader

    async def _run_post_crawl_pipeline(self, client, channel_username, crawl_result=None):
        async with self._pipeline_lock:
            # Anything that fails Advertio after this cutoff is deferred until
            # the next scheduler cycle. This prevents immediate same-cycle hammering.
            advertio_cycle_cutoff = datetime.now(timezone.utc).isoformat()
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
            elif classification_stats.get("stopped"):
                print(f"{Fore.YELLOW}⚠️ AI category classification skipped by operator; remaining records stay pending.")
            else:
                print(f"{Fore.GREEN}✅ AI category classification completed | Found: {classification_stats['found']} | Processed: {classification_stats['processed']} | Skipped: {classification_stats['skipped']} | Failed: {classification_stats['failed']}")
            await self.monitor.report("classification", {k: v for k, v in classification_stats.items() if k != "disabled"})
            self.stage_control.consume_skip("ai")

            extraction_stats = {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": False, "advertio": None}
            if not classification_stats.get("disabled") and not classification_stats.get("stopped"):
                print(f"\n{Fore.CYAN}▸ Starting category-based AI data extraction...")
                self._bind_media_downloader(
                    self.ai_processing,
                    client,
                    asyncio.get_running_loop(),
                )
                extraction_stats = await asyncio.to_thread(
                    self.ai_processing.process_pending_with_stats,
                    should_stop=lambda: self.stage_control.is_skip_requested("ai"),
                )
                if extraction_stats.get("disabled"):
                    print(f"{Fore.YELLOW}⚠️ AI data extraction is disabled in configuration.")
                elif extraction_stats.get("stopped"):
                    print(f"{Fore.YELLOW}⚠️ AI data extraction skipped by operator; remaining records stay pending.")
                else:
                    print(f"{Fore.GREEN}✅ AI data extraction completed | Found: {extraction_stats['found']} | Processed: {extraction_stats['processed']} | Skipped: {extraction_stats['skipped']} | Failed: {extraction_stats['failed']}")
                await self.monitor.report("ai", {k: v for k, v in extraction_stats.items() if k != "disabled"})
                self.stage_control.consume_skip("ai")
            else:
                reason = "disabled" if classification_stats.get("disabled") else "stopped"
                print(f"{Fore.YELLOW}⚠️ AI data extraction not started because classification stage is {reason}.")

            advertio_stats = {"found": 0, "sent": 0, "already_existed": 0, "failed": 0}
            advertio_service = getattr(self.ai_processing, "advertio_service", None)
            if config.ADVERTIO_INGEST_ENABLED and advertio_service is not None:
                if self.stage_control.is_skip_requested("advertio"):
                    print(
                        f"{Fore.YELLOW}⚠️ Advertio stage skipped by operator; "
                        "waiting/retry records remain pending for the next cycle."
                    )
                else:
                    print(f"\n{Fore.CYAN}▸ Starting automatic Advertio waiting/retry delivery...")
                    media_downloader = self._make_media_downloader(
                        client,
                        asyncio.get_running_loop(),
                    )
                    advertio_stats = await asyncio.to_thread(
                        advertio_service.deliver_pending,
                        limit=100,
                        channel_username=None,
                        progress=True,
                        media_downloader=media_downloader,
                        before_datetime=advertio_cycle_cutoff,
                    )
                    print(
                        f"{Fore.GREEN}✅ Advertio delivery completed | "
                        f"Found: {advertio_stats['found']} | Sent: {advertio_stats['sent']} | "
                        f"Already existed: {advertio_stats['already_existed']} | "
                        f"Failed: {advertio_stats['failed']}"
                    )
                    await self.monitor.report("advertio", advertio_stats)
                self.stage_control.consume_skip("advertio")

            return processing_stats, classification_stats, extraction_stats

    @staticmethod
    def _normalize_crawl_stats(result):
        if isinstance(result, dict):
            return dict(result)
        stats = {}
        for name in ("saved", "media_saved", "media_failed", "filtered", "bot_skipped", "no_username", "weak_text", "skipped", "from_date", "to_date", "stopped", "status"):
            if hasattr(result, name):
                stats[name] = getattr(result, name)
        return stats or {"status": "completed"}

    async def _run_cycle(
        self,
        client,
        channels,
        interval_minutes,
        from_date,
        to_date,
        crawl_mode,
        channel_interval_minutes=0,
    ):
        """Run one complete multi-channel cycle before waiting for the next cycle.

        All selected channels are crawled once per cycle in a newly randomized
        order. Only after the final channel finishes do the processing/AI stages
        run and the transfer-live summary get sent once. The scheduler then
        waits for the next cycle.
        """
        first_cycle = True
        while True:
            try:
                if first_cycle:
                    cycle_from_date, cycle_to_date = from_date, to_date
                    print(
                        f"[SCHEDULER] Initial full crawl cycle: "
                        f"{cycle_from_date} -> {cycle_to_date}"
                    )
                else:
                    today = date.today()
                    cycle_from_date, cycle_to_date = max(from_date, today), today
                    print(
                        f"[SCHEDULER] New full crawl cycle: "
                        f"{cycle_from_date} -> {cycle_to_date}"
                    )

                completed = 0
                # Shuffle a copy for every full cycle. Keep the selected channel
                # list unchanged so scheduling identity and future cycles remain
                # stable, and visit every selected channel exactly once.
                cycle_channels = list(channels)
                random.shuffle(cycle_channels)
                total = len(cycle_channels)

                for index, channel_username in enumerate(cycle_channels, start=1):
                    if index > 1 and channel_interval_minutes > 0:
                        print(
                            f"[SCHEDULER] Waiting {channel_interval_minutes:g} minute(s) "
                            f"before channel {index}/{total}: @{channel_username}"
                        )
                        await asyncio.sleep(channel_interval_minutes * 60)

                    print(
                        f"[SCHEDULER] Crawling channel {index}/{total}: "
                        f"@{channel_username}"
                    )
                    crawl_result = await self.crawl_job.run_channel(
                        client,
                        channel_username,
                        cycle_from_date,
                        cycle_to_date,
                        crawl_mode=crawl_mode,
                        should_stop=lambda: self.stage_control.is_skip_requested("crawl"),
                    )

                    crawl_stats = self._normalize_crawl_stats(crawl_result)
                    crawl_stats.setdefault("channel", f"@{channel_username}")
                    await self.monitor.report("crawl", crawl_stats)
                    self.stage_control.consume_skip("crawl")
                    completed += 1

                    if isinstance(crawl_result, dict) and crawl_result.get("stopped"):
                        print(
                            f"{Fore.YELLOW}⚠️ CRAWL STOPPED BY OPERATOR: "
                            f"@{channel_username}"
                        )
                        break

                print(
                    f"{Fore.GREEN}✅ FULL CRAWL CYCLE COMPLETED: "
                    f"{completed}/{total} channel(s)"
                )

                if completed == total:
                    await self._run_post_crawl_pipeline(
                        client,
                        f"{total} channels",
                        {"status": "completed", "channels": total},
                    )
                    print(f"{Fore.CYAN}▸ Sending automatic transfer-live summary...")
                    try:
                        await self.monitor.broadcast_transfer_live()
                        print(f"{Fore.GREEN}✅ Automatic transfer-live summary sent.")
                    except Exception as exc:
                        await self.monitor.error(
                            "ERROR",
                            "services.scheduler_service",
                            f"Automatic transfer-live report failed: {exc}",
                        )
                else:
                    print(
                        f"{Fore.YELLOW}⚠️ Full crawl cycle was incomplete; "
                        "automatic transfer-live summary was not sent."
                    )

                first_cycle = False
                next_run_at = time.monotonic() + interval_minutes * 60
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[COLLECTION] Full crawl cycle failed: {exc}")
                await self.monitor.error(
                    "ERROR",
                    "services.scheduler_service",
                    f"Full crawl cycle failed: {exc}",
                )
                first_cycle = False
                next_run_at = time.monotonic() + interval_minutes * 60

            sleep_seconds = max(0, next_run_at - time.monotonic())
            print(
                f"[COLLECTION] Next full crawl cycle in "
                f"{sleep_seconds / 60:g} minute(s)."
            )
            await asyncio.sleep(sleep_seconds)

    def schedule_cycle(
        self,
        client,
        channels,
        from_date,
        to_date,
        interval_minutes=None,
        channel_interval_minutes=0,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        if from_date > to_date:
            raise ValueError("Start date cannot be later than To date")

        normalized_channels = []
        seen = set()
        for channel in channels or []:
            username = str(channel or "").strip()
            key = username.lower().lstrip("@")
            if not key or key in seen:
                continue
            seen.add(key)
            normalized_channels.append(username)

        if not normalized_channels:
            raise ValueError("At least one channel must be selected")

        interval = float(
            interval_minutes
            if interval_minutes is not None
            else config.CRAWL_INTERVAL_MINUTES
        )
        if interval <= 0:
            raise ValueError("Crawler interval must be greater than zero")

        spacing = float(channel_interval_minutes or 0)
        if spacing < 0:
            raise ValueError("Channel interval cannot be negative")

        session = getattr(getattr(client, "session", None), "filename", None)
        session_name = session or str(getattr(client, "session", client))
        key = (
            f"{session_name}:cycle:{','.join(c.lower().lstrip('@') for c in normalized_channels)}:"
            f"{from_date}:{to_date}:{crawl_mode}"
        )
        existing = self._tasks.get(key)
        if existing and not existing.done():
            return existing

        task = asyncio.create_task(
            self._run_cycle(
                client,
                normalized_channels,
                interval,
                from_date,
                to_date,
                crawl_mode,
                channel_interval_minutes=spacing,
            )
        )
        self._tasks[key] = task
        return task

    def schedule_channel(
        self,
        client,
        channel_username,
        from_date=None,
        to_date=None,
        interval_minutes=None,
        start_delay_minutes=None,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        """Legacy single-channel scheduler API.

        It remains available for compatibility. New multi-channel scheduling
        should use schedule_cycle() so one report is emitted per full cycle.
        """
        if from_date is None or to_date is None:
            today = date.today()
            from_date = from_date or today
            to_date = to_date or today
        return self.schedule_cycle(
            client,
            [channel_username],
            from_date,
            to_date,
            interval_minutes=interval_minutes,
            channel_interval_minutes=0,
            crawl_mode=crawl_mode,
        )

    def active_jobs(self):
        return {k: v for k, v in self._tasks.items() if not v.done()}

    def stop_all(self):
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
