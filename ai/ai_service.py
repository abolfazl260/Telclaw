"""Orchestrates Groq extraction from the independent AI queue."""

from datetime import datetime, timezone

import config
from ai.extractor import GroqExtractor
from storage.message_repository import MessageRepository


class AIProcessingService:
    def __init__(self, repository=None, extractor=None):
        self.repository = repository or MessageRepository()
        self.extractor = extractor or GroqExtractor()

    @staticmethod
    def _source_text(record):
        """Return the only Telegram payload allowed to reach the AI provider."""
        return (
            record.get("cleaned_text")
            or record.get("text")
            or record.get("raw_text")
            or ""
        ).strip()

    def _process(self, records, *, progress=False):
        total = len(records)
        processed = failed = skipped = 0

        for index, record in enumerate(records, start=1):
            source_text = self._source_text(record)

            # Messages without usable text are not AI failures and must never
            # trigger a request to Groq. They leave the queue as skipped.
            if not source_text:
                self.repository.mark_ai_skipped(
                    record["message_id"],
                    record["channel_username"],
                    reason="no_text",
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                skipped += 1
                if progress:
                    print(
                        f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) "
                        "skipped: no_text"
                    )
                continue

            self.repository.mark_ai_processing(
                record["message_id"], record["channel_username"]
            )
            try:
                # source_text is the only message content sent to the extractor.
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
        return processed, failed, skipped

    def process_pending(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "disabled": True}

        records = self.repository.get_ai_pending(
            limit=limit, channel_username=channel_username
        )
        processed, failed, skipped = self._process(records)
        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "disabled": False,
        }

    def process_pending_with_stats(self, limit=100, channel_username=None):
        """Consume only the AI queue after processing has completed successfully."""
        if not config.AI_EXTRACTION_ENABLED:
            print("[AI] Extraction disabled; skipping AI queue.")
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "disabled": True}

        records = self.repository.get_ai_pending(
            limit=limit, channel_username=channel_username
        )
        if not records:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "disabled": False}

        processed, failed, skipped = self._process(records, progress=True)
        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "disabled": False,
        }
