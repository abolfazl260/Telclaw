"""Backward-compatible scheduling facade for the collection layer.

The console UI still calls ``start_crawler`` for each channel.  In the
architecture-refactor branch that call now starts a long-running background
crawl job: the channel is crawled immediately and then crawled again at the
configured interval.
"""

import asyncio
from datetime import datetime, timezone

import config

from collection.crawler import crawl_channel


_scheduled_tasks = {}


def _task_key(client, channel_username):
    session = getattr(client, "session", None)
    session_name = getattr(session, "filename", None) or str(session)
    return f"{session_name}:{channel_username.lower().lstrip('@')}"


async def _scheduled_channel_crawl(client, channel_username, interval_hours):
    """Run one channel immediately and repeat it forever at the given interval."""
    while True:
        started_at = datetime.now(timezone.utc)
        target_date = started_at.date()

        try:
            await crawl_channel(client, channel_username, target_date)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"\n[SCHEDULER] Crawl failed for {channel_username}: {exc}. "
                "The next scheduled run will continue normally."
            )

        print(
            f"\n[SCHEDULER] Next crawl for {channel_username} "
            f"in {interval_hours:g} hour(s)."
        )
        await asyncio.sleep(interval_hours * 60 * 60)


async def start_crawler(client, channel_username, target_date=None):
    """Start a persistent background crawl job for a channel.

    ``target_date`` is retained for backward compatibility with the existing
    UI. Scheduled runs always use the current UTC date so a job can continue
    across midnight without keeping a stale date from its first run.
    """
    key = _task_key(client, channel_username)
    existing = _scheduled_tasks.get(key)

    if existing is not None and not existing.done():
        print(f"[SCHEDULER] Already scheduled: {channel_username}")
        return existing

    interval_hours = float(getattr(config, "CRAWL_INTERVAL_HOURS", 5))
    if interval_hours <= 0:
        raise ValueError("CRAWL_INTERVAL_HOURS must be greater than zero")

    task = asyncio.create_task(
        _scheduled_channel_crawl(
            client,
            channel_username,
            interval_hours,
        )
    )
    _scheduled_tasks[key] = task

    print(
        f"[SCHEDULER] Scheduled {channel_username} every "
        f"{interval_hours:g} hour(s). First crawl starts now."
    )
    return task


def get_scheduled_jobs():
    """Return currently active scheduled channel jobs."""
    return {
        key: task
        for key, task in _scheduled_tasks.items()
        if not task.done()
    }


def stop_all():
    """Cancel all scheduled crawl jobs."""
    for task in _scheduled_tasks.values():
        if not task.done():
            task.cancel()
    _scheduled_tasks.clear()


__all__ = ["start_crawler", "get_scheduled_jobs", "stop_all"]
