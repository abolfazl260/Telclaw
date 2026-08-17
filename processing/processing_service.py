"""Application service for deterministic message processing.

Processing is intentionally independent from Telegram collection. It reads
raw messages from SQLite, transforms them, and writes only derived fields.
Raw Telegram content is never overwritten.
"""

from datetime import datetime, timezone

from processing.contracts import ProcessingRecord
from processing.stages import CleanTextStage, NormalizeStage, Pipeline
from storage.message_repository import MessageRepository


class ProcessingService:
    """Run the first deterministic processing stages over collected messages."""

    def __init__(self, repository=None, pipeline=None):
        self.repository = repository or MessageRepository()
        self.pipeline = pipeline or Pipeline([NormalizeStage(), CleanTextStage()])

    def process_pending(self, limit=500, channel_username=None):
        records = self.repository.get_pending(
            limit=limit, channel_username=channel_username
        )
        processed = 0
        failed = 0

        for data in records:
            try:
                record = self.pipeline.process(ProcessingRecord(data=dict(data)))
                result = record.data
                self.repository.mark_processed(
                    message_id=result["message_id"],
                    channel_username=result["channel_username"],
                    cleaned_text=result.get("cleaned_text", ""),
                    text=result.get("cleaned_text", ""),
                    processing_status="processed",
                    pipeline_version="processing-v1",
                    cleaned_at=datetime.now(timezone.utc).isoformat(),
                )
                processed += 1
            except Exception:
                failed += 1

        return {"found": len(records), "processed": processed, "failed": failed}
