"""Application-facing boundary for Telegram collection."""

from collection.crawler import crawl_channel


class CollectionService:
    """Expose collection use-cases without leaking crawler implementation."""

    async def crawl_channel(self, client, channel_username, target_date):
        return await crawl_channel(client, channel_username, target_date)
