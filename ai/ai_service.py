"""Orchestrates AI extraction, validation, and category persistence."""

from datetime import datetime, timezone

import config
from ai.extractor import AIExtractionError, OpenAIExtractor
from storage.message_repository import MessageRepository


class AIProcessingService:
    def __init__(self, repository=None, extractor=None):
        self.repository = repository or MessageRepository()
        self.extractor = extractor or OpenAIExtractor()

    def process_pending(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": True}

        records = self.repository.get_ai_pending(limit=limit, channel_username=channel_username)
        processed = failed = 0
        for record in records:
            try:
                source_text = record.get("cleaned_text") or record.get("text") or record.get("raw_text") or ""
                if not source_text.strip():
                    raise AIExtractionError("Processed message has no text to extract")
                category, data = self.extractor.extract(source_text)
                self.repository.save_category_record(record["id"], category, data)
                self.repository.mark_processed(
                    message_id=record["message_id"],
                    channel_username=record["channel_username"],
                    processing_status="ai_processed",
                    ai_category=category,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                    ai_error=None,
                )
                processed += 1
            except Exception as exc:
                self.repository.mark_processed(
                    message_id=record["message_id"],
                    channel_username=record["channel_username"],
                    processing_status="ai_failed",
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                    ai_error=str(exc)[:2000],
                )
                failed += 1
        return {"found": len(records), "processed": processed, "failed": failed, "skipped": False}
