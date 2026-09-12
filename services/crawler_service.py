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
        return self.schedule_categories(
            client,
            [category],
            from_date,
            to_date,
            interval_minutes=interval_minutes,
            channel_interval_minutes=channel_interval_minutes,
            crawl_mode=crawl_mode,
        )

    def schedule_categories(
        self,
        client,
        categories,
        from_date,
        to_date,
        interval_minutes=None,
        channel_interval_minutes=None,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        """Schedule crawling for one or more categories.

        Channels shared by multiple selected categories are scheduled only once.
        The order of categories and channels is preserved.
        """
        if from_date > to_date:
            raise ValueError("Start date cannot be later than end date")

        if isinstance(categories, str):
            categories = [categories]

        selected_categories = list(dict.fromkeys(categories or []))
        if not selected_categories:
            raise ValueError("At least one category must be selected")

        spacing = (
            float(channel_interval_minutes)
            if channel_interval_minutes is not None
            else float(config.CHANNEL_INTERVAL_MINUTES)
        )
        if spacing < 0:
            raise ValueError("Channel interval cannot be negative")

        jobs = []
        scheduled_usernames = set()

        for category in selected_categories:
            for channel in self.channels_for_category(category):
                username = channel.get("username")
                if not username or username in scheduled_usernames:
                    continue

                scheduled_usernames.add(username)
                jobs.append(
                    self.scheduler.schedule_channel(
                        client,
                        username,
                        from_date,
                        to_date,
                        interval_minutes=interval_minutes,
                        start_delay_minutes=spacing * len(jobs),
                        crawl_mode=crawl_mode,
                    )
                )

        return jobs

    def active_jobs(self):
        return self.scheduler.active_jobs()

    def stop_all(self):
        self.scheduler.stop_all()
