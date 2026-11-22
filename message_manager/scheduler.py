"""Independent runtime worker; SQLite owns all durable execution state."""
import asyncio
from datetime import datetime
import logging
import time

import aiohttp
import config
from .models import SendResult, utcnow
from .telegram_sender import PreflightRetry
from .validation import ValidationError

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60
RECOVERY_INTERVAL_SECONDS = 300


class Worker:
    def __init__(self, repo, sender):
        self.repo, self.sender = repo, sender
        self._last_heartbeat_at = None
        self._last_recovery_at = None

    def _heartbeat_if_due(self):
        now = time.monotonic()
        if (self._last_heartbeat_at is None
                or now - self._last_heartbeat_at >= HEARTBEAT_INTERVAL_SECONDS):
            self.repo.worker_heartbeat()
            self._last_heartbeat_at = now

    def _recover_if_due(self):
        now = time.monotonic()
        if (self._last_recovery_at is None
                or now - self._last_recovery_at >= RECOVERY_INTERVAL_SECONDS):
            self.repo.recover()
            self._last_recovery_at = now

    def _idle_sleep_seconds(self):
        """Sleep up to the idle poll interval, waking early for a scheduled post."""
        delay = config.MESSAGE_MANAGER_POLL_SECONDS
        next_due = self.repo.next_schedule_due()
        if next_due:
            until_due = (datetime.fromisoformat(next_due) - utcnow()).total_seconds()
            if until_due > 0:
                delay = min(delay, max(0.5, until_due))
        return delay

    async def tick(self):
        self._heartbeat_if_due()
        # Recovery must still run at startup and periodically, even when idle.
        self._recover_if_due()
        # Do not open write transactions for materialize/claim on empty queues.
        if not self.repo.has_due_work():
            return 0

        self.repo.materialize(limit=config.MESSAGE_MANAGER_BATCH_SIZE,
            once_grace=config.MESSAGE_MANAGER_MISSED_WINDOW_SECONDS,
            recurring_grace=config.MESSAGE_MANAGER_RECURRING_GRACE_SECONDS)
        handled = 0
        for _ in range(config.MESSAGE_MANAGER_BATCH_SIZE):
            job = self.repo.claim(spacing=config.MESSAGE_MANAGER_SEND_SPACING_SECONDS,
                once_grace=config.MESSAGE_MANAGER_MISSED_WINDOW_SECONDS,
                recurring_grace=config.MESSAGE_MANAGER_RECURRING_GRACE_SECONDS)
            if not job:
                break
            target = next((t for t in self.repo.targets() if t['id'] == job['target_id']), None)
            try:
                if not target or not target['mm_eligible'] or target['chat_id'] != job['chat_id']:
                    result = SendResult('failed', 'Destination changed or is no longer eligible')
                else:
                    await self.sender.check_target(target)
                    # Cancellation/eligibility may have changed during the permission request.
                    with self.repo.connection() as c:
                        allowed = c.execute("""SELECT 1 FROM mm_deliveries d JOIN mm_occurrences o ON o.id=d.occurrence_id
                         JOIN mm_schedules s ON s.id=o.schedule_id JOIN mm_targets t ON t.target_id=d.target_id
                         JOIN publishing_targets p ON p.id=d.target_id WHERE d.id=? AND d.status='sending'
                         AND s.state IN ('active','completed') AND t.eligible=1 AND p.chat_id=d.chat_id""", (job['id'],)).fetchone()
                    result = await self.sender.send(job) if allowed else SendResult('failed', 'Publication cancelled/paused before send')
            except PreflightRetry as exc:
                result = SendResult('retry', str(exc), retry_after=exc.retry_after)
            except ValidationError as exc:
                result = SendResult('failed', str(exc)[:1000])
            except (aiohttp.ClientError, TimeoutError):
                # No publication method has been called when a permission check fails.
                result = SendResult('retry', 'Permission check temporarily unavailable')
            except asyncio.CancelledError:
                # Leave durable sending claim. Expiry marks it uncertain, never automatically retries.
                raise
            except Exception as exc:
                logger.error('Message Manager delivery failed | delivery=%s type=%s', job['id'], type(exc).__name__)
                result = SendResult('uncertain', 'Unexpected dispatch error; verify Telegram before resending')
            self.repo.finish(job, result, max_attempts=config.MESSAGE_MANAGER_MAX_ATTEMPTS)
            handled += 1
            self._heartbeat_if_due()
        return handled

    async def run(self):
        cycles = 0
        while True:
            handled = 0
            try:
                handled = await self.tick()
                if cycles % 1000 == 0:
                    self.sender.media.purge_unreferenced(config.MESSAGE_MANAGER_MEDIA_RETENTION_DAYS)
                cycles += 1
            except asyncio.CancelledError:
                self.repo.worker_heartbeat('stopped')
                raise
            except Exception as exc:
                self.repo.worker_heartbeat('error', type(exc).__name__)
                # A successful next tick should promptly restore the running state.
                self._last_heartbeat_at = None
                logger.exception('Message Manager worker cycle failed')
            try:
                # Preserve short spacing when the worker is actively draining
                # deliveries; use slower, due-aware polling when idle.
                delay = (config.MESSAGE_MANAGER_SEND_SPACING_SECONDS
                         if handled else self._idle_sleep_seconds())
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self.repo.worker_heartbeat('stopped')
                raise
