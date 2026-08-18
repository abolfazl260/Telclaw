"""Orchestrates one scheduled crawl cycle."""

from datetime import datetime, timezone

from collection.crawler import CRAWL_MODE_ALL
from collection.collector_service import CollectionService
from services.processing_service import ProcessingService


class CrawlJobService:
    """Coordinates Collection -> Processing for one channel cycle."""

    def __init__(self, collection_service=None, processing_service=None):
        self.collection = collection_service or CollectionService()
        self.processing = processing_service or ProcessingService()

    async def run_channel(
        self,
        client,
        channel_username,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        await self.collection.crawl_channel(
            client,
            channel_username,
            datetime.now(timezone.utc).date(),
            crawl_mode=crawl_mode,
        )
        return self.processing.process_pending(channel_username=channel_username)
