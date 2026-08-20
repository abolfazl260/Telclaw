"""Application service for deterministic message processing.

Processing is intentionally independent from Telegram collection. It reads
raw messages from SQLite, transforms them, and writes only derived fields.
Raw Telegram content is never overwritten.

Duplicate removal is performed from the original crawled Telegram payload
before cleaning/normalization. A later message from the same sender is
removed when its original text is at least 80% similar to an earlier message.
"""

from datetime import datetime, timezone
from difflib import SequenceMatcher
import re

from processing.contracts import ProcessingRecord
from processing.stages import CleanTextStage, NormalizeStage, Pipeline
from storage.message_repository import MessageRepository


DUPLICATE_SIMILARITY_THRESHOLD = 0.80


class ProcessingService:
    """Run deterministic processing and raw-message duplicate removal."""

    def __init__(self, repository=None, pipeline=None):
        self.repository = repository or MessageRepository()
        self.pipeline = pipeline or Pipeline([NormalizeStage(), CleanTextStage()])

    @staticmethod
    def _original_text(data):
        """Return the original crawled text; never use cleaned text for dedupe."""
        return (data.get("raw_text") or data.get("text") or "").strip()

    @staticmethod
    def _similarity_text(text):
        """Normalize only for comparison; the stored/crawled text is untouched."""
        return re.sub(r"\s+", " ", text).strip().casefold()

    @classmethod
    def _similarity(cls, left, right):
        left = cls._similarity_text(left)
        right = cls._similarity_text(right)
        if not left or not right:
            return 0.0
        return SequenceMatcher(None, left, right).ratio()

    def _is_duplicate(self, record, seen_by_sender):
        """Check a record against earlier crawled records for the same sender."""
        sender_id = record.get("sender_id")
        if sender_id is None:
            # Never make a fuzzy duplicate decision without a stable Telegram user ID.
            return False

        original_text = self._original_text(record)
        if not original_text:
            return False

        normalized_sender = str(sender_id)
        for previous in seen_by_sender.get(normalized_sender, []):
            previous_text = previous["text"]
            similarity = self._similarity(original_text, previous_text)
            if similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
                return True
        return False

    def process_pending(self, limit=500, channel_username=None):
        records = self.repository.get_pending(
            limit=limit, channel_username=channel_username
        )
        processed = 0
        failed = 0
        duplicates_removed = 0
        seen_by_sender = {}

        for data in records:
            original_text = self._original_text(data)
            sender_id = data.get("sender_id")

            try:
                # Dedupe is deliberately based on the original crawled payload,
                # before NormalizeStage/CleanTextStage modifies anything.
                if self._is_duplicate(data, seen_by_sender):
                    self.repository.delete_message(
                        message_id=data["message_id"],
                        channel_username=data["channel_username"],
                    )
                    duplicates_removed += 1
                    continue

                if sender_id is not None and original_text:
                    seen_by_sender.setdefault(str(sender_id), []).append(
                        {"text": original_text, "message_id": data["message_id"]}
                    )

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

        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "duplicates_removed": duplicates_removed,
        }
