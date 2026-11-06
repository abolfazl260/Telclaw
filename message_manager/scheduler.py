"""Independent runtime worker; SQLite owns all durable execution state."""
import asyncio
import logging

import aiohttp
import config
from .models import SendResult
from .telegram_sender import PreflightRetry
from .validation import ValidationError

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, repo, sender):
        self.repo, self.sender = repo, sender

    async def tick(self):
        self.repo.recover()
        self.repo.materialize(limit=config.MESSAGE_MANAGER_BATCH_SIZE,
            once_grace=config.MESSAGE_MANAGER_MISSED_WINDOW_SECONDS,
            recurring_grace=config.MESSAGE_MANAGER_RECURRING_GRACE_SECONDS)
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

    async def run(self):
        cycles = 0
        while True:
            try:
                await self.tick()
                if cycles % 1000 == 0:
                    self.sender.media.purge_unreferenced(config.MESSAGE_MANAGER_MEDIA_RETENTION_DAYS)
                cycles += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Message Manager worker cycle failed')
            await asyncio.sleep(config.MESSAGE_MANAGER_POLL_SECONDS)
