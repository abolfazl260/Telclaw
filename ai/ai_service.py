"""Orchestrates Groq extraction from the independent AI queue."""

from datetime import datetime, timezone
import logging

import config
from ai.extractor import AIExtractionError, GroqExtractor
from storage.message_repository import MessageRepository


logger = logging.getLogger("telclaw.ai")


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

    def _log_ai_error(self, record, exc):
        """Keep complete provider diagnostics in logs, never in message storage."""
        logger.error(
            "[AI ERROR] provider=%s status=%s model=%s reason=%s message_id=%s channel=%s detail=%s",
            getattr(exc, "provider", "groq"),
            getattr(exc, "status", None),
            getattr(self.extractor, "model", None),
            getattr(exc, "reason", None),
            record.get("message_id"),
            record.get("channel_username"),
            str(exc),
            exc_info=True,
        )

    def _process(self, records, *, progress=False):
        total = len(records)
        processed = failed = skipped = 0
        stopped = False

        for index, record in enumerate(records, start=1):
            source_text = self._source_text(record)

            # Messages without usable text are data-quality skips, not provider failures.
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
                category, data = self.extractor.extract(source_text)
                self.repository.save_category_record(record["id"], category, data)
                self.repository.mark_ai_result(
                    message_id=record["message_id"],
                    channel_username=record["channel_username"],
                    success=True,
                    ai_category=category,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                processed += 1
                if progress:
                    print(
                        f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) "
                        f"processed -> {category}"
                    )
            except AIExtractionError as exc:
                self._log_ai_error(record, exc)
                self.repository.mark_ai_result(
                    message_id=record["message_id"],
                    channel_username=record["channel_username"],
                    success=False,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                failed += 1
                if progress:
                    print(
                        f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) "
                        f"failed: {getattr(exc, 'reason', 'ai_error')}"
                    )

                # A 403 is an access/configuration problem shared by the queue.
                # Stop immediately so all remaining records stay pending and can
                # be retried after the model/API permission is fixed.
                if exc.stop_queue:
                    stopped = True
                    logger.error(
                        "[AI QUEUE STOPPED] provider=groq status=403 model=%s reason=%s; remaining messages stay pending",
                        self.extractor.model,
                        getattr(exc, "reason", "permissions_error"),
                    )
                    if progress:
                        print(
                            "[AI] queue stopped after provider permission error; "
                            "remaining messages remain pending"
                        )
                    break
            except Exception as exc:
                # Unexpected service errors are also diagnostic-only. Never copy
                # arbitrary exception text into the message row.
                logger.exception(
                    "[AI ERROR] provider=groq model=%s reason=unexpected_error message_id=%s channel=%s",
                    self.extractor.model,
                    record.get("message_id"),
                    record.get("channel_username"),
                )
                self.repository.mark_ai_result(
                    message_id=record["message_id"],
                    channel_username=record["channel_username"],
                    success=False,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                failed += 1
                if progress:
                    print(
                        f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) "
                        "failed: unexpected_error"
                    )

        return processed, failed, skipped, stopped

    def process_pending(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True}

        records = self.repository.get_ai_pending(
            limit=limit, channel_username=channel_username
        )
        processed, failed, skipped, stopped = self._process(records)
        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "stopped": stopped,
            "disabled": False,
        }

    def process_pending_with_stats(self, limit=100, channel_username=None):
        """Consume only the AI queue after processing has completed successfully."""
        if not config.AI_EXTRACTION_ENABLED:
            print("[AI] Extraction disabled; skipping AI queue.")
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True}

        records = self.repository.get_ai_pending(
            limit=limit, channel_username=channel_username
        )
        if not records:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": False}

        processed, failed, skipped, stopped = self._process(records, progress=True)
        return {
            "found": len(records),
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "stopped": stopped,
            "disabled": False,
        }
