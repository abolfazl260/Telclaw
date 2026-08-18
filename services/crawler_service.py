"""Crawler application service.

Coordinates category/channel selection and scheduling without exposing UI
or Telegram implementation details to callers.
"""

from collection.crawler import CRAWL_MODE_ALL
from services.channel_service import ChannelService
from services.scheduler_service import SchedulerService


class CrawlerService:
    def __init__(self, channel_service=None, scheduler_service=None):
        self.channels = channel_service or ChannelService()
        self.scheduler = scheduler_service or SchedulerService()

    def categories(self):
        return self.channels.categories()

    def channels_for_category(self, category):
        return self.channels.channels_for_category(category)

    def schedule_category(
        self,
        client,
        category,
        interval_hours=None,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        channels = self.channels_for_category(category)
        jobs = []
        for channel in channels:
            username = channel.get("username")
            if not username:
                continue
            jobs.append(
                self.scheduler.schedule_channel(
                    client,
                    username,
                    interval_hours=interval_hours,
                    crawl_mode=crawl_mode,
                )
            )
        return jobs

    def active_jobs(self):
        return self.scheduler.active_jobs()

    def stop_all(self):
        self.scheduler.stop_all()
