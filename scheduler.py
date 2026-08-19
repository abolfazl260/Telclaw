"""Compatibility facade for the scheduler application service."""

from services.scheduler_service import SchedulerService


_service = SchedulerService()


async def start_crawler(client, channel_username, target_date=None):
    """Keep the legacy API while delegating scheduling to the service layer."""
    return _service.schedule_channel(client, channel_username)


def get_scheduled_jobs():
    return _service.active_jobs()


def stop_all():
    _service.stop_all()


__all__ = ["start_crawler", "get_scheduled_jobs", "stop_all"]
