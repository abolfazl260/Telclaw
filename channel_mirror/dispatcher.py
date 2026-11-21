"""Finite, crawl-triggered queue drains. There is no live/polling sender loop."""

import asyncio
import logging

import config
from delivery import bot_limits
from .bot_api import BotAPI
from .models import SendOutcome
from .telegram_sender import Sender

logger = logging.getLogger("telclaw.channel_mirror")


class Dispatcher:
    def __init__(self, repo, sender=None, *, batch_size=50):
        self.repo = repo
        self.sender = sender or Sender(repo, BotAPI(config.TELEGRAM_BOT_TOKEN))
        self.batch_size = batch_size
        self.tasks = {}
        self.pending = {}

    def trigger(self, event, client):
        peer = event["source_peer_id"]
        self.pending[peer] = client
        task = self.tasks.get(peer)
        if task is None or task.done():
            task = asyncio.create_task(self._run(peer), name=f"channel-mirror:{peer}")
            self.tasks[peer] = task
            task.add_done_callback(self._finished)
        return task

    async def _run(self, peer):
        while peer in self.pending:
            client = self.pending.pop(peer)
            await self.drain(peer, client)

    @staticmethod
    def _finished(task):
        if not task.cancelled() and task.exception():
            logger.warning("Mirror drain failed; durable outbox remains pending: %s", type(task.exception()).__name__)

    async def drain(self, peer, client):
        self.repo.recover_expired()
        events = self.repo.pending_dispatches(1000, source_peer_id=peer)
        if not events:
            return 0
        processed = 0
        # Claims are durable and peer-scoped; independent dispatchers may not
        # double-send. No sleeping until future retry due times or future crawls.
        for _ in range(self.batch_size):
            if bot_limits.cooldown_seconds(config.TELEGRAM_BOT_TOKEN):
                break
            job = self.repo.claim(source_peer_id=peer)
            if not job:
                break
            try:
                result = await asyncio.wait_for(self.sender.send(job, client), 240)
            except asyncio.CancelledError:
                self.repo.finish(job["id"], job["claim_token"], SendOutcome("uncertain", error="Dispatch cancelled; verify Telegram"))
                raise
            except Exception:
                result = SendOutcome("uncertain", error="Unexpected dispatch interruption; verify Telegram")
            self.repo.finish(job["id"], job["claim_token"], result)
            processed += 1
            if result.retry_after:
                break
        # Events are hints, not delivery ownership. Retry/waiting items remain
        # durable and are drained by the next successful source crawl.
        for event in events:
            self.repo.acknowledge_dispatch(event["id"])
        return processed

    async def stop(self):
        self.cancel()
        tasks = list(self.tasks.values())
        await asyncio.gather(*tasks, return_exceptions=True)

    def cancel(self):
        self.pending.clear()
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
