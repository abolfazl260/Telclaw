"""Orchestrates Groq extraction from the independent AI queue."""

from datetime import datetime, timezone
import logging
import re
import time

import config
from ai.extractor import AIExtractionError, GroqExtractor
from storage.message_repository import MessageRepository

logger = logging.getLogger("telclaw.ai")


class AIProcessingService:
    def __init__(self, repository=None, extractor=None, advertio_service=None):
        self.repository = repository or MessageRepository()
        self.extractor = extractor or GroqExtractor()
        self.advertio_service = advertio_service
        if self.advertio_service is None and config.ADVERTIO_INGEST_ENABLED:
            from delivery.advertio_service import AdvertioDeliveryService
            self.advertio_service = AdvertioDeliveryService()

    @staticmethod
    def _source_text(record):
        return (record.get("cleaned_text") or record.get("text") or record.get("raw_text") or "").strip()

    def _log_ai_error(self, record, exc):
        logger.error(
            "[AI ERROR] provider=%s status=%s model=%s reason=%s message_id=%s channel=%s detail=%s",
            getattr(exc, "provider", "groq"), getattr(exc, "status", None), getattr(self.extractor, "model", None),
            getattr(exc, "reason", None), record.get("message_id"), record.get("channel_username"), str(exc), exc_info=True,
        )

    @staticmethod
    def _rate_limit_wait(exc, retry_number):
        """Wait conservatively for Groq TPM limits before retrying a request."""
        text = str(exc)
        match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", text, re.IGNORECASE)
        if match:
            suggested = float(match.group(1))
            wait = max(suggested, config.GROQ_RATE_LIMIT_MIN_WAIT_SECONDS)
        else:
            # 20s -> 40s -> 80s -> 120s, then remain capped at the configured maximum.
            wait = config.GROQ_RATE_LIMIT_MIN_WAIT_SECONDS * (2 ** max(0, retry_number - 1))

        wait = min(wait, config.GROQ_RATE_LIMIT_MAX_WAIT_SECONDS)
        print(
            f"[AI] Groq rate limit reached; waiting {wait:.1f}s "
            f"before retry {retry_number}/{config.GROQ_RATE_LIMIT_MAX_RETRIES}..."
        )
        time.sleep(wait)

    def _extract_with_retry(self, source_text, record=None, progress=False):
        attempts = 0
        while True:
            try:
                return self.extractor.extract(source_text)
            except AIExtractionError as exc:
                if getattr(exc, "status", None) != 429 or attempts >= config.GROQ_RATE_LIMIT_MAX_RETRIES:
                    raise
                attempts += 1
                self._rate_limit_wait(exc, attempts)

    def _deliver_to_advertio(self, record, category, data):
        if not self.advertio_service or category != "housinglist":
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            result = self.advertio_service.deliver(record, data)
            status = "already_existed" if result.get("already_existed") else "sent"
            self.repository.mark_advertio_result(
                record["message_id"], record["channel_username"],
                status=status, lead_id=result.get("lead_id"), error=None, processed_at=now,
            )
            print(f"[ADVERTIO] {status}: message={record['message_id']} lead={result.get('lead_id')} http={result.get('http_status')}")
        except Exception as exc:
            from delivery.advertio_client import AdvertioError
            retryable = isinstance(exc, AdvertioError) and exc.retryable
            status = "retry" if retryable else "rejected"
            self.repository.mark_advertio_result(
                record["message_id"], record["channel_username"],
                status=status, lead_id=getattr(exc, "lead_id", None), error=str(exc)[:4000], processed_at=now,
            )
            logger.error(
                "[ADVERTIO ERROR] status=%s retryable=%s message_id=%s detail=%s",
                getattr(exc, "status", None), retryable, record.get("message_id"), str(exc), exc_info=True,
            )
            print(f"[ADVERTIO] {status}: message={record['message_id']} reason={str(exc)[:300]}")

    def _process(self, records, *, progress=False):
        total = len(records)
        processed = failed = skipped = 0
        stopped = False

        for index, record in enumerate(records, start=1):
            source_text = self._source_text(record)
            if not source_text:
                self.repository.mark_ai_skipped(
                    record["message_id"], record["channel_username"], reason="no_text",
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                skipped += 1
                if progress:
                    print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) skipped: no_text")
                continue

            self.repository.mark_ai_processing(record["message_id"], record["channel_username"])
            try:
                category, data = self._extract_with_retry(source_text, record, progress=progress)
                self.repository.save_category_record(record["id"], category, data)
                self.repository.mark_ai_result(
                    message_id=record["message_id"], channel_username=record["channel_username"], success=True,
                    ai_category=category, ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                self._deliver_to_advertio(record, category, data)
                processed += 1
                if progress:
                    print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) processed -> {category}")
            except AIExtractionError as exc:
                self._log_ai_error(record, exc)
                self.repository.mark_ai_result(
                    message_id=record["message_id"], channel_username=record["channel_username"], success=False,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                failed += 1
                if progress:
                    print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) failed: {getattr(exc, 'reason', 'ai_error')}")
                if exc.stop_queue:
                    stopped = True
                    logger.error(
                        "[AI QUEUE STOPPED] provider=groq status=403 model=%s reason=%s; remaining messages stay pending",
                        self.extractor.model, getattr(exc, "reason", "permissions_error"),
                    )
                    if progress:
                        print("[AI] queue stopped after provider permission error; remaining messages remain pending")
                    break
            except Exception:
                logger.exception(
                    "[AI ERROR] provider=groq model=%s reason=unexpected_error message_id=%s channel=%s",
                    self.extractor.model, record.get("message_id"), record.get("channel_username"),
                )
                self.repository.mark_ai_result(
                    message_id=record["message_id"], channel_username=record["channel_username"], success=False,
                    ai_processed_at=datetime.now(timezone.utc).isoformat(),
                )
                failed += 1
                if progress:
                    print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) failed: unexpected_error")

        return processed, failed, skipped, stopped

    def process_pending(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True}
        records = self.repository.get_ai_pending(limit=limit, channel_username=channel_username)
        processed, failed, skipped, stopped = self._process(records)
        return {"found": len(records), "processed": processed, "failed": failed, "skipped": skipped, "stopped": stopped, "disabled": False}

    def process_pending_with_stats(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            print("[AI] Extraction disabled; skipping AI queue.")
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True}
        records = self.repository.get_ai_pending(limit=limit, channel_username=channel_username)
        if not records:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": False}
        processed, failed, skipped, stopped = self._process(records, progress=True)
        return {"found": len(records), "processed": processed, "failed": failed, "skipped": skipped, "stopped": stopped, "disabled": False}
