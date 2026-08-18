"""Application-facing boundary for Telegram collection."""

from collection.crawler import CRAWL_MODE_ALL, crawl_channel


class CollectionService:
    """Expose collection use-cases without leaking crawler implementation."""

    async def crawl_channel(
        self,
        client,
        channel_username,
        target_date,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        return await crawl_channel(
            client,
            channel_username,
            target_date,
            crawl_mode=crawl_mode,
        )
