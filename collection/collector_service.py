"""Application-facing boundary for Telegram collection."""

from collection.crawler import CRAWL_MODE_ALL, crawl_channel


class CollectionService:
    """Expose collection use-cases without leaking crawler implementation."""

    async def crawl_channel(
        self,
        client,
        channel_username,
        from_date,
        to_date,
        crawl_mode=CRAWL_MODE_ALL,
        should_stop=None,
        mirror_capture=None,
        incremental=False,
        checkpoint_floor=None,
    ):
        options = {"mirror_capture": mirror_capture} if mirror_capture is not None else {}
        if incremental:
            options.update(incremental=True, checkpoint_floor=checkpoint_floor)
        return await crawl_channel(
            client,
            channel_username,
            from_date,
            to_date,
            crawl_mode=crawl_mode,
            should_stop=should_stop,
            **options,
        )
