"""Orchestrates one collection cycle only.

Processing and AI are independent workers and are intentionally not executed
as part of a crawler run. Their database queues enforce pipeline order.
"""

import asyncio
import inspect
import logging

from channel_mirror import Repository
from channel_mirror.capture import Capture, CaptureStopped
from collection.crawler import CRAWL_MODE_ALL
from collection.collector_service import CollectionService
from services.telegram_session_guard import session_guard

logger = logging.getLogger("telclaw.channel_mirror")


class CrawlJobService:
    """Coordinates only the collection queue for one channel cycle."""

    def __init__(self, collection_service=None, mirror_repository=None, dispatch_trigger=None):
        self.collection = collection_service or CollectionService()
        # Capture is best-effort: never wait seconds for another SQLite writer.
        self.mirror = Repository(busy_timeout_ms=250) if mirror_repository is None else mirror_repository
        self.dispatch_trigger = dispatch_trigger
        self._mirror_captures = {}
        self._scheduled_sources = {}
        self._dispatcher = None

    async def stop_mirror(self):
        if self._dispatcher:
            await self._dispatcher.stop()

    def cancel_mirror(self):
        if self._dispatcher:
            self._dispatcher.cancel()

    def register_schedule(self, key, sources):
        self._scheduled_sources[key] = tuple(sources)

    def unregister_schedule(self, key):
        self._scheduled_sources.pop(key, None)

    def mirror_status(self):
        sources = [s for group in self._scheduled_sources.values() for s in group]
        peers = {c.source_peer_id for c in self._mirror_captures.values() if c.active}
        return self.mirror.crawl_status(sources, peers)

    def _begin_mirror(self, channel, from_date, to_date, crawl_mode, should_stop):
        try:
            if self.mirror and self.mirror.has_active_rules():
                run_id = self.mirror.begin_crawl(channel, from_date, to_date, crawl_mode)
                capture = Capture(self.mirror, run_id, channel, should_stop)
                self._mirror_captures[run_id] = capture
                return capture
        except Exception:
            logger.warning("[MIRROR CAPTURE] unavailable for source=%s", channel, exc_info=True)
        return None

    async def run_channel(
        self,
        client,
        channel_username,
        from_date,
        to_date,
        crawl_mode=CRAWL_MODE_ALL,
        should_stop=None,
        incremental=False,
        checkpoint_floor=None,
    ):
        print(f"\n{'=' * 60}")
        print(f"📥 COLLECTION: {channel_username}")
        print(f"{'=' * 60}")

        capture = self._begin_mirror(channel_username, from_date, to_date, crawl_mode, should_stop)
        options = {"mirror_capture": capture} if capture is not None else {}
        if incremental:
            # A failed mirror setup must never cause history to be skipped.
            try:
                safe = capture is not None or not self.mirror or not self.mirror.has_active_rules()
            except Exception:
                safe = False
            if safe:
                options.update(incremental=True, checkpoint_floor=checkpoint_floor)
        try:
            async with session_guard(client):
                result = await self.collection.crawl_channel(
                    client, channel_username, from_date, to_date,
                    crawl_mode=crawl_mode, should_stop=should_stop, **options,
                )
            if capture is None:
                return result
            stopped = (isinstance(result, dict) and result.get("stopped")) or (should_stop and should_stop())
            completed = isinstance(result, dict) and result.get("status") == "completed"
            summary = {"run_id": capture.run_id, "state": "mirror_failed", "deliveries_created": 0}
            if stopped or not completed:
                summary["state"] = "stopped" if stopped else "failed"
                capture.abort(summary["state"])
            elif not capture.error:
                try:
                    async with session_guard(client):
                        await capture.prepare_albums()
                    capture.check_stop()
                    summary = self.mirror.complete_crawl(capture.run_id)
                except CaptureStopped:
                    summary["state"] = "stopped"
                    capture.abort("stopped")
                except Exception as exc:
                    capture.fail(exc)
                else:
                    # The durable event was committed with all deliveries. This
                    # optional nonblocking wake-up never sends or runs AI here.
                    if summary["dispatch"]:
                        try:
                            value = None
                            if self.dispatch_trigger:
                                value = self.dispatch_trigger(summary["dispatch"])
                            else:
                                import config
                                if config.TELEGRAM_BOT_TOKEN:
                                    from channel_mirror.dispatcher import Dispatcher
                                    if self._dispatcher is None:
                                        self._dispatcher = Dispatcher(self.mirror)
                                    self._dispatcher.trigger(summary["dispatch"], client)
                            if inspect.isawaitable(value):
                                if inspect.iscoroutine(value):
                                    value.close()
                                raise TypeError("Dispatch trigger must be synchronous and nonblocking")
                        except Exception:
                            summary["dispatch_error"] = True
                            logger.warning("[MIRROR DISPATCH] wake-up failed; durable event remains pending", exc_info=True)
            if isinstance(result, dict):
                return {**result, "mirror": summary}
            return result
        except asyncio.CancelledError:
            if capture:
                capture.abort("interrupted", "Crawl task cancelled")
            raise
        except BaseException:
            if capture:
                capture.abort("failed", "Crawl did not complete")
            raise
        finally:
            if capture:
                self._mirror_captures.pop(capture.run_id, None)
