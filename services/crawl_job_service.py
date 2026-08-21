"""Orchestrates one collection cycle only.

Processing and AI are independent workers and are intentionally not executed
as part of a crawler run. Their database queues enforce pipeline order.
"""

from collection.crawler import CRAWL_MODE_ALL
from collection.collector_service import CollectionService


class CrawlJobService:
    """Coordinates only the collection queue for one channel cycle."""

    def __init__(self, collection_service=None):
        self.collection = collection_service or CollectionService()

    async def run_channel(
        self,
        client,
        channel_username,
        from_date,
        to_date,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        print(f"\n{'=' * 60}")
        print(f"📥 COLLECTION: {channel_username}")
        print(f"{'=' * 60}")

        return await self.collection.crawl_channel(
            client,
            channel_username,
            from_date,
            to_date,
            crawl_mode=crawl_mode,
        )
