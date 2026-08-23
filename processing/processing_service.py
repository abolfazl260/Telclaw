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
import logging
import re

from processing.contracts import ProcessingRecord
from processing.stages import CleanTextStage, NormalizeStage, Pipeline
from storage.message_repository import MessageRepository


DUPLICATE_SIMILARITY_THRESHOLD = 0.80
logger = logging.getLogger("telclaw.processing")


class ProcessingService:
    """Run deterministic processing and raw-message duplicate removal."""

    def __init__(self, repository=None, pipeline=None):
        self.repository = repository or MessageRepository()
        self.pipeline = pipeline or Pipeline([NormalizeStage(), CleanTextStage()])

    @staticmethod
    def _original_text(data):
        """Return only the original crawled Telegram payload for dedupe."""
        return (data.get("raw_text") or data.get("text") or "").strip()

    @staticmethod
    def _similarity_text(text):
        """Normalize only the comparison copy; never modify stored raw text."""
        return re.sub(r"\s+", " ", text).strip().casefold()

    @classmethod
    def _similarity(cls, left, right):
        left = cls._similarity_text(left)
        right = cls._similarity_text(right)
        if not left or not right:
            return 0.0
        return SequenceMatcher(None, left, right).ratio()

    def _is_duplicate(self, record):
        """Compare against every earlier crawled message from the same sender."""
        sender_id = record.get("sender_id")
        if sender_id is None:
            return False, 0.0

        original_text = self._original_text(record)
        if not original_text:
            return False, 0.0

        previous_messages = self.repository.get_previous_messages_by_sender(
            sender_id=sender_id,
            before_id=record["id"],
        )
        best_similarity = 0.0
        for previous in previous_messages:
            previous_text = self._original_text(previous)
            similarity = self._similarity(original_text, previous_text)
            best_similarity = max(best_similarity, similarity)
            if similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
                return True, similarity
        return False, best_similarity

    @staticmethod
    def _log_failure(data, exc, *, reason="processing_exception"):
        logger.error(
            "[PROCESSING][FAILED] message_id=%s channel=%s reason=%s error_type=%s detail=%s",
            data.get("message_id"),
            data.get("channel_username"),
            reason,
            type(exc).__name__,
            str(exc) or "<empty>",
            exc_info=True,
        )

    def process_pending(self, limit=500, channel_username=None):
        records = self.repository.get_pending(
            limit=limit, channel_username=channel_username
        )
        processed = 0
        failed = 0
        duplicates_removed = 0

        for data in records:
            try:
                is_duplicate, similarity = self._is_duplicate(data)
                if is_duplicate:
                    self.repository.delete_message(
                        message_id=data["message_id"],
                        channel_username=data["channel_username"],
                    )
                    duplicates_removed += 1
                    logger.info(
                        "[PROCESSING][DUPLICATE_REMOVED] message_id=%s channel=%s sender_id=%s similarity=%.2f%%",
                        data.get("message_id"),
                        data.get("channel_username"),
                        data.get("sender_id"),
                        similarity * 100,
                    )
                    print(
                        "[PROCESS] duplicate removed: "
                        f"message={data['message_id']} "
                        f"channel={data['channel_username']} "
                        f"sender={data.get('sender_id')} "
                        f"similarity={similarity * 100:.2f}%"
                    )
                    continue

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
            except Exception as exc:
                failed += 1
                self._log_failure(data, exc)
                print(
                    "[PROCESS] failed: "
                    f"message={data.get('message_id')} "
                    f"channel={data.get('channel_username')} "
                    f"reason=processing_exception "
                    f"error={type(exc).__name__}: {exc}"
                )

        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "duplicates_removed": duplicates_removed,
        }
