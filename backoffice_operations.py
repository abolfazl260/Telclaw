"""Back Office operational controller mirroring the terminal/TUI commands.

The web layer stays thin: this controller calls the same application services used
by SystemConsoleUI and runs long queue operations as background asyncio tasks.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime, timezone

import config
from ai.ai_service import AIProcessingService
from ai.classification_service import CategoryClassificationService
from ai.groq_connection_test import test_groq_connection
from collection.crawler import CRAWL_MODE_ALL, CRAWL_MODE_PHOTOS_ONLY
from collection.media_downloader import download_photos_for_record
from delivery.advertio_service import AdvertioDeliveryService, AdvertioMappingError
from delivery.telegram_transfer import get_transfer_queue_status
from services.account_service import AccountService
from services.channel_service import ChannelService
from services.crawler_service import CrawlerService
from services.processing_service import ProcessingService
from storage import database


RUNNABLE_PIPELINE_JOBS = ("processing", "classification", "ai", "advertio", "groq")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class BackofficeOperations:
    """Coordinate manual operations without duplicating queue business logic."""

    def __init__(self, console_ui=None):
        self.console_ui = console_ui
        self._processing = getattr(console_ui, "processing_service", None)
        self._classification = getattr(console_ui, "classification_service", None)
        self._ai = getattr(console_ui, "ai_service", None)
        self._advertio = getattr(console_ui, "advertio_service", None)
        self._crawler = getattr(console_ui, "crawler", None)
        self._accounts = getattr(console_ui, "accounts", None)
        self._channels = getattr(console_ui, "channels", None)
        self._client = None
        self._account_name = getattr(console_ui, "client_account", None)
        self._tasks = {}
        self._states = {}
        self._stop_events = {}
        self._fallback_pipeline_lock = asyncio.Lock()

    @property
    def processing(self):
        if self._processing is None:
            self._processing = ProcessingService()
        return self._processing

    @property
    def classification(self):
        if self._classification is None:
            self._classification = CategoryClassificationService()
        return self._classification

    @property
    def ai(self):
        if self._ai is None:
            self._ai = AIProcessingService()
        return self._ai

    @property
    def accounts(self):
        if self._accounts is None:
            self._accounts = AccountService()
        return self._accounts

    @property
    def channels(self):
        if self._channels is None:
            self._channels = ChannelService(config.CHANNELS_JSON)
        return self._channels

    @property
    def crawler(self):
        if self._crawler is None:
            self._crawler = CrawlerService(self.channels)
        return self._crawler

    @property
    def advertio(self):
        if self._advertio is None and config.ADVERTIO_INGEST_ENABLED:
            try:
                self._advertio = AdvertioDeliveryService()
            except AdvertioMappingError:
                return None
        return self._advertio

    @property
    def pipeline_lock(self):
        scheduler = getattr(self.crawler, "scheduler", None)
        lock = getattr(scheduler, "_pipeline_lock", None)
        return lock or self._fallback_pipeline_lock

    def _activity(self, kind, level, message, details=None):
        try:
            database.record_system_activity(
                kind,
                level=level,
                source="backoffice.operations",
                message=message,
                details=details,
            )
        except Exception:
            # Operational controls must not fail merely because reporting failed.
            pass

    def state(self, name):
        state = self._states.get(name)
        if state is None:
            return {
                "name": name,
                "status": "idle",
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
                "requested_by": None,
                "params": {},
            }
        return dict(state)

    def states(self):
        return {name: self.state(name) for name in RUNNABLE_PIPELINE_JOBS}

    def any_running(self):
        return any(self.state(name)["status"] in {"queued", "running", "stopping"}
                   for name in RUNNABLE_PIPELINE_JOBS)

    def _start_background(self, name, requested_by, params, operation):
        existing = self._tasks.get(name)
        if existing is not None and not existing.done():
            raise RuntimeError(f"{name} is already running")

        stop_event = threading.Event()
        self._stop_events[name] = stop_event
        self._states[name] = {
            "name": name,
            "status": "queued",
            "started_at": _now_iso(),
            "finished_at": None,
            "result": None,
            "error": None,
            "requested_by": int(requested_by) if requested_by is not None else None,
            "params": dict(params or {}),
        }

        async def runner():
            state = self._states[name]
            state["status"] = "running"
            self._activity(
                f"manual_{name}",
                "INFO",
                f"Manual {name} started from Back Office",
                {"requested_by": state["requested_by"], **state["params"]},
            )
            try:
                result = await operation(stop_event)
                state["result"] = result
                stopped = bool(
                    stop_event.is_set()
                    or (isinstance(result, dict) and result.get("stopped"))
                )
                state["status"] = "stopped" if stopped else "completed"
                self._activity(
                    f"manual_{name}",
                    "INFO" if not stopped else "WARNING",
                    f"Manual {name} {state['status']}",
                    result,
                )
            except asyncio.CancelledError:
                state["status"] = "stopped"
                state["error"] = "cancelled"
                self._activity(
                    f"manual_{name}",
                    "WARNING",
                    f"Manual {name} cancelled",
                )
                raise
            except Exception as exc:
                state["status"] = "failed"
                state["error"] = f"{exc.__class__.__name__}: {exc}"[:4000]
                self._activity(
                    f"manual_{name}",
                    "ERROR",
                    f"Manual {name} failed: {exc}",
                )
            finally:
                state["finished_at"] = _now_iso()
                self._stop_events.pop(name, None)

        task = asyncio.create_task(runner(), name=f"backoffice-{name}")
        self._tasks[name] = task
        return self.state(name)

    def request_stop(self, name):
        task = self._tasks.get(name)
        event = self._stop_events.get(name)
        if task is None or task.done() or event is None:
            return False
        event.set()
        if name in self._states:
            self._states[name]["status"] = "stopping"
        return True

    def start_processing(self, requested_by=None):
        async def operation(stop_event):
            async with self.pipeline_lock:
                return await asyncio.to_thread(
                    self.processing.process_pending_with_stats,
                    should_stop=stop_event.is_set,
                )

        return self._start_background(
            "processing", requested_by, {}, operation
        )

    def start_classification(self, batch_size=None, requested_by=None):
        batch_size = int(batch_size or config.AI_CLASSIFICATION_BATCH_SIZE)
        if batch_size <= 0:
            raise ValueError("Batch size must be a positive integer")

        async def operation(stop_event):
            async with self.pipeline_lock:
                return await asyncio.to_thread(
                    self.classification.process_pending_with_stats,
                    batch_size,
                    should_stop=stop_event.is_set,
                )

        return self._start_background(
            "classification",
            requested_by,
            {"batch_size": batch_size},
            operation,
        )

    async def retry_failed_classifications(self, requested_by=None):
        if self.state("classification")["status"] in {"queued", "running", "stopping"}:
            raise RuntimeError("classification is already running")
        retried = await asyncio.to_thread(
            self.classification.repository.retry_failed_classifications
        )
        self._activity(
            "classification_retry",
            "INFO",
            f"Requeued {retried} failed classification(s) from Back Office",
            {"requested_by": requested_by, "retried": retried},
        )
        return retried

    async def _connect_account(self, account_name):
        account_name = str(account_name or "").strip()
        if not account_name:
            raise ValueError("Select a Telegram account")

        if self.console_ui is not None:
            client = await self.console_ui.connect_client(account_name)
            if client is None:
                raise RuntimeError(f"Unable to connect Telegram account '{account_name}'")
            self._account_name = getattr(self.console_ui, "client_account", None) or account_name
            return client

        if self._client is not None and self._account_name == account_name:
            return self._client
        if self._client is not None:
            await self.accounts.disconnect(self._client)
            self._client = None
            self._account_name = None
        self._client = await self.accounts.connect(account_name)
        self._account_name = account_name
        return self._client

    async def connect_account(self, account_name, requested_by=None):
        client = await self._connect_account(account_name)
        self._activity(
            "account_selected",
            "INFO",
            f"Telegram account '{self._account_name}' selected in Back Office",
            {"requested_by": requested_by},
        )
        return bool(client)

    async def disconnect_account(self, requested_by=None):
        if self.console_ui is not None:
            client = getattr(self.console_ui, "client", None)
            if client is not None:
                await self.accounts.disconnect(client)
                self.console_ui.client = None
                if hasattr(self.console_ui, "client_account"):
                    self.console_ui.client_account = None
        elif self._client is not None:
            await self.accounts.disconnect(self._client)
            self._client = None
        old = self._account_name
        self._account_name = None
        if old:
            self._activity(
                "account_disconnected",
                "INFO",
                f"Telegram account '{old}' disconnected from Back Office",
                {"requested_by": requested_by},
            )

    def connected_account(self):
        if self.console_ui is not None:
            return getattr(self.console_ui, "client_account", None) or self._account_name
        return self._account_name

    async def list_accounts(self):
        return await self.accounts.list_accounts()

    def _make_sync_media_downloader(self, client):
        loop = asyncio.get_running_loop()

        def download(record):
            future = asyncio.run_coroutine_threadsafe(
                download_photos_for_record(client, record),
                loop,
            )
            return future.result()

        return download

    def start_ai(self, account_name, requested_by=None):
        async def operation(stop_event):
            client = await self._connect_account(account_name)
            self.ai.set_media_downloader(self._make_sync_media_downloader(client))
            async with self.pipeline_lock:
                return await asyncio.to_thread(
                    self.ai.process_pending_with_stats,
                    should_stop=stop_event.is_set,
                )

        return self._start_background(
            "ai",
            requested_by,
            {"account": str(account_name or "")},
            operation,
        )

    def start_advertio(self, limit=100, account_name=None, requested_by=None):
        if not config.ADVERTIO_INGEST_ENABLED:
            raise RuntimeError("Advertio ingestion is disabled")
        service = self.advertio
        if service is None:
            raise RuntimeError("Advertio is enabled but its configuration is incomplete")
        limit = int(limit)
        if limit <= 0:
            raise ValueError("Advertio limit must be a positive integer")

        async def operation(_stop_event):
            preview = await asyncio.to_thread(
                service.repository.get_advertio_pending,
                limit=limit,
                channel_username=None,
            )
            media_downloader = None
            if any(record.get("media_type") == "photo" for record in preview):
                client = await self._connect_account(account_name)
                media_downloader = self._make_sync_media_downloader(client)
            async with self.pipeline_lock:
                return await asyncio.to_thread(
                    service.deliver_pending,
                    limit=limit,
                    progress=True,
                    media_downloader=media_downloader,
                )

        return self._start_background(
            "advertio",
            requested_by,
            {"limit": limit, "account": str(account_name or "")},
            operation,
        )

    def start_groq_test(self, requested_by=None):
        async def operation(_stop_event):
            success = await asyncio.to_thread(test_groq_connection)
            return {"success": bool(success)}

        return self._start_background("groq", requested_by, {}, operation)

    async def start_crawler(
        self,
        *,
        account_name,
        categories,
        from_date,
        to_date,
        interval_minutes,
        crawl_mode=CRAWL_MODE_ALL,
        requested_by=None,
    ):
        categories = [str(value) for value in (categories or []) if str(value).strip()]
        if not categories:
            raise ValueError("Select at least one crawler category")
        if crawl_mode not in {CRAWL_MODE_ALL, CRAWL_MODE_PHOTOS_ONLY}:
            raise ValueError("Invalid crawl mode")
        if not isinstance(from_date, date) or not isinstance(to_date, date):
            raise ValueError("Crawler dates are invalid")
        if from_date > to_date:
            raise ValueError("From date cannot be after To date")
        interval_minutes = float(interval_minutes)
        if interval_minutes <= 0:
            raise ValueError("Crawler interval must be greater than zero")

        client = await self._connect_account(account_name)
        jobs = self.crawler.schedule_categories(
            client,
            categories,
            from_date,
            to_date,
            interval_minutes=interval_minutes,
            crawl_mode=crawl_mode,
        )
        details = {
            "requested_by": requested_by,
            "account": account_name,
            "categories": categories,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "interval_minutes": interval_minutes,
            "crawl_mode": crawl_mode,
            "jobs": len(jobs),
        }
        self._activity(
            "crawler_schedule",
            "INFO",
            f"Scheduled crawler for {len(categories)} category group(s)",
            details,
        )
        return details

    def stop_crawler(self, requested_by=None):
        active = len(self.crawler.active_jobs())
        self.crawler.stop_all()
        self._activity(
            "crawler_stop",
            "WARNING",
            f"Stopped {active} active crawler job(s) from Back Office",
            {"requested_by": requested_by, "stopped_jobs": active},
        )
        return active

    def crawler_status(self):
        return {
            "active_jobs": len(self.crawler.active_jobs()),
            "job_keys": list(self.crawler.active_jobs().keys()),
        }

    def channel_data(self):
        return self.channels.load()

    def classification_status(self):
        status = self.classification.repository.get_classification_queue_status()
        status["eligible_pending"] = len(
            self.classification.repository.get_classification_pending(limit=100000)
        )
        return status

    def transfer_status(self):
        return get_transfer_queue_status()

    async def begin_account_registration(self, session_name, phone):
        return await self.accounts.begin_registration(session_name, phone)

    async def submit_account_code(self, session_name, code):
        return await self.accounts.submit_registration_code(session_name, code)

    async def submit_account_password(self, session_name, password):
        return await self.accounts.submit_registration_password(session_name, password)

    async def cancel_account_registration(self, session_name):
        return await self.accounts.cancel_registration(session_name)

    def account_registration_state(self, session_name=None):
        return self.accounts.registration_state(session_name)

    async def close(self):
        for name in list(self._stop_events):
            self.request_stop(name)
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.console_ui is None and self._client is not None:
            await self.accounts.disconnect(self._client)
            self._client = None
            self._account_name = None


__all__ = ["BackofficeOperations", "RUNNABLE_PIPELINE_JOBS"]
