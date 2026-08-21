"""Crawler application service.

Coordinates category/channel selection and scheduling without exposing UI
or Telegram implementation details to callers.
"""

from collection.crawler import CRAWL_MODE_ALL
import config
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
        from_date,
        to_date,
        interval_minutes=None,
        channel_interval_minutes=None,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        if from_date > to_date:
            raise ValueError("Start date cannot be later than end date")

        channels = self.channels_for_category(category)
        spacing = (
            float(channel_interval_minutes)
            if channel_interval_minutes is not None
            else float(config.CHANNEL_INTERVAL_MINUTES)
        )
        if spacing < 0:
            raise ValueError("Channel interval cannot be negative")

        jobs = []
        for index, channel in enumerate(channels):
            username = channel.get("username")
            if not username:
                continue
            jobs.append(
                self.scheduler.schedule_channel(
                    client,
                    username,
                    from_date,
                    to_date,
                    interval_minutes=interval_minutes,
                    start_delay_minutes=spacing * index,
                    crawl_mode=crawl_mode,
                )
            )
        return jobs

    def active_jobs(self):
        return self.scheduler.active_jobs()

    def stop_all(self):
        self.scheduler.stop_all()
