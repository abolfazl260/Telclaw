"""Orchestrates one crawl cycle."""

from collection.crawler import CRAWL_MODE_ALL
from collection.collector_service import CollectionService
from ai.ai_service import AIProcessingService
from services.processing_service import ProcessingService


class CrawlJobService:
    """Coordinates Collection -> Cleaning/Processing -> AI for one channel cycle."""

    def __init__(self, collection_service=None, processing_service=None, ai_service=None):
        self.collection = collection_service or CollectionService()
        self.processing = processing_service or ProcessingService()
        self.ai = ai_service or AIProcessingService()

    async def run_channel(
        self,
        client,
        channel_username,
        from_date,
        to_date,
        crawl_mode=CRAWL_MODE_ALL,
    ):
        print(f"\n{'=' * 60}")
        print(f"🔄 PIPELINE STARTED: {channel_username}")
        print(f"{'=' * 60}")

        await self.collection.crawl_channel(
            client,
            channel_username,
            from_date,
            to_date,
            crawl_mode=crawl_mode,
        )

        print(f"\n{'=' * 60}")
        print(f"🧹 PROCESSING / CLEANING: {channel_username}")
        print(f"{'=' * 60}")
        print("ℹ️ Previously processed messages are automatically skipped.")

        total_processed = 0
        total_processing_failed = 0
        while True:
            stats = self.processing.process_pending_with_stats(
                channel_username=channel_username
            )
            total_processed += stats["processed"]
            total_processing_failed += stats["failed"]
            if stats["found"] == 0:
                break
            if stats["processed"] == 0:
                break

        print("\n✅ PROCESSING COMPLETED")
        print(f"   Processed: {total_processed}")
        print(f"   Failed:    {total_processing_failed}")
        print("   Skipped:   already-processed records excluded from the queue")

        print(f"\n{'=' * 60}")
        print(f"🤖 AI PROCESSING / GROQ: {channel_username}")
        print(f"{'=' * 60}")
        print("ℹ️ Only successfully processed messages are sent to AI.")
        print("ℹ️ Messages already marked ai_processed are automatically skipped.")

        total_ai_processed = 0
        total_ai_failed = 0
        while True:
            stats = self.ai.process_pending_with_stats(
                channel_username=channel_username
            )
            total_ai_processed += stats["processed"]
            total_ai_failed += stats["failed"]
            if stats["skipped"] or stats["found"] == 0:
                break
            if stats["processed"] == 0:
                break

        print("\n✅ AI PROCESSING COMPLETED")
        print(f"   Sent to AI: {total_ai_processed}")
        print(f"   AI failed:  {total_ai_failed}")
        print("   Skipped:    already AI-processed records excluded from the queue")

        return {
            "channel": channel_username,
            "processing": {
                "processed": total_processed,
                "failed": total_processing_failed,
            },
            "ai": {
                "processed": total_ai_processed,
                "failed": total_ai_failed,
            },
        }
