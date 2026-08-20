"""Orchestrates Groq extraction from the independent AI queue."""

from datetime import datetime, timezone

import config
from ai.extractor import AIExtractionError, GroqExtractor
from storage.message_repository import MessageRepository


class AIProcessingService:
    def __init__(self, repository=None, extractor=None):
        self.repository = repository or MessageRepository()
        self.extractor = extractor or GroqExtractor()

    def _process(self, records, *, progress=False):
        total = len(records)
        processed = failed = 0

        for index, record in enumerate(records, start=1):
            self.repository.mark_ai_processing(
                record["message_id"], record["channel_username"]
            )
            try:
                source_text = (
                    record.get("cleaned_text")
                    or record.get("text")
                    or record.get("raw_text")
                    or ""
                )
                if not source_text.strip():
                    raise AIExtractionError("Processed message has no text to extract")

                category, data = self.extractor.extract(source_text)
                self.repository.save_category_record(record["id"], category, data)
                self.repository.mark_ai_result(
                    message_id=record["message_id"],
                    channel_username=record["channel_username"],
                    success=True,
                    ai_category=category,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                    ai_error=None,
                )
                processed += 1
                if progress:
                    print(
                        f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) "
                        f"processed -> {category}"
                    )
            except Exception as exc:
                self.repository.mark_ai_result(
                    message_id=record["message_id"],
                    channel_username=record["channel_username"],
                    success=False,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                    ai_error=str(exc)[:2000],
                )
                failed += 1
                if progress:
                    print(
                        f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) "
                        f"failed: {exc}"
                    )
        return processed, failed

    def process_pending(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": True}

        records = self.repository.get_ai_pending(
            limit=limit, channel_username=channel_username
        )
        processed, failed = self._process(records)
        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "skipped": False,
        }

    def process_pending_with_stats(self, limit=100, channel_username=None):
        """Consume only the AI queue after processing has completed successfully."""
        if not config.AI_EXTRACTION_ENABLED:
            print("[AI] Extraction disabled; skipping AI queue.")
            return {"found": 0, "processed": 0, "failed": 0, "skipped": True}

        records = self.repository.get_ai_pending(
            limit=limit, channel_username=channel_username
        )
        if not records:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": False}

        processed, failed = self._process(records, progress=True)
        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "skipped": False,
        }
