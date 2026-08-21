"""Orchestrates Groq extraction from the independent AI queue."""

from datetime import datetime, timezone
import logging
import random
import re
import time

import config
from ai.extractor import AIExtractionError, GroqExtractor
from storage.message_repository import MessageRepository

logger = logging.getLogger("telclaw.ai")


class AIProcessingService:
    def __init__(self, repository=None, extractor=None, advertio_service=None, media_downloader=None):
        self.repository = repository or MessageRepository()
        self.extractor = extractor or GroqExtractor()
        self.advertio_service = advertio_service
        self.media_downloader = media_downloader
        if self.advertio_service is None and config.ADVERTIO_INGEST_ENABLED:
            from delivery.advertio_service import AdvertioDeliveryService
            self.advertio_service = AdvertioDeliveryService()

    def set_media_downloader(self, media_downloader):
        """Set the existing Telegram media download operation for this AI run."""
        self.media_downloader = media_downloader

    @staticmethod
    def _source_text(record):
        return (record.get("cleaned_text") or record.get("text") or record.get("raw_text") or "").strip()

    def _log_ai_error(self, record, exc):
        logger.error("[AI ERROR] provider=%s status=%s model=%s reason=%s message_id=%s channel=%s detail=%s", getattr(exc, "provider", "groq"), getattr(exc, "status", None), getattr(self.extractor, "model", None), getattr(exc, "reason", None), record.get("message_id"), record.get("channel_username"), str(exc), exc_info=True)

    @staticmethod
    def _parse_wait_from_error(text):
        if not text:
            return None
        match = re.search(r"try again in\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?", text, re.IGNORECASE)
        if match:
            return float(match.group(1) or 0) * 60 + float(match.group(2) or 0)
        match = re.search(r"retry[- ]after[:=\s]+(\d+(?:\.\d+)?)\s*s?", text, re.IGNORECASE)
        return float(match.group(1)) if match else None

    def _rate_limit_wait(self, exc, retry_number):
        provider_wait = getattr(exc, "retry_after", None) or self._parse_wait_from_error(str(exc))
        if provider_wait is not None:
            wait, reason = max(0.0, float(provider_wait)), "provider_retry_after"
        else:
            base = min(2 ** retry_number, config.GROQ_RATE_LIMIT_MAX_WAIT_SECONDS)
            wait = max(config.GROQ_RATE_LIMIT_MIN_WAIT_SECONDS, min(base + random.uniform(0, min(1.0, base * 0.25)), config.GROQ_RATE_LIMIT_MAX_WAIT_SECONDS))
            reason = "exponential_backoff_jitter"
        logger.warning("[GROQ RATE LIMIT] provider=groq model=%s status=429 retry=%s/%s wait=%.2fs reason=%s", getattr(self.extractor, "model", None), retry_number, config.GROQ_RATE_LIMIT_MAX_RETRIES, wait, reason)
        print(f"[AI] Groq rate limit: model={getattr(self.extractor, 'model', None)} status=429 retry={retry_number}/{config.GROQ_RATE_LIMIT_MAX_RETRIES} wait={wait:.2f}s reason={reason}")
        time.sleep(wait)

    def _extract_with_retry(self, source_text, record, progress=False):
        attempts = 0
        while True:
            try:
                return self.extractor.extract(source_text)
            except AIExtractionError as exc:
                if getattr(exc, "status", None) != 429 or attempts >= config.GROQ_RATE_LIMIT_MAX_RETRIES:
                    raise
                attempts += 1
                self._rate_limit_wait(exc, attempts)

    def _prepare_media_for_advertio(self, record):
        """Download only required media after AI acceptance and before Advertio."""
        if record.get("media_type") != "photo":
            return True

        existing = record.get("media_path")
        if existing:
            from pathlib import Path
            if Path(existing).is_file():
                return True

        if self.media_downloader is None:
            raise RuntimeError("Telegram media downloader is not configured for this AI run")

        media_path = self.media_downloader(record)
        if not media_path:
            raise RuntimeError("Telegram media download returned no file")

        record["media_path"] = media_path
        return True

    def _deliver_to_advertio(self, record, category, data):
        if not self.advertio_service or category != "housinglist":
            return {"attempted": 0, "sent": 0, "already_existed": 0, "failed": 0}
        now = datetime.now(timezone.utc).isoformat()

        # Media preparation is a distinct retryable step. AI remains successful
        # when Telegram media retrieval fails; only Advertio delivery is retried.
        try:
            self._prepare_media_for_advertio(record)
        except Exception as exc:
            self.repository.mark_advertio_result(record["message_id"], record["channel_username"], status="retry", lead_id=None, error=str(exc)[:4000], processed_at=now)
            logger.error("[MEDIA ERROR] message_id=%s channel=%s detail=%s", record.get("message_id"), record.get("channel_username"), str(exc), exc_info=True)
            print(f"[MEDIA] retry: message={record['message_id']} reason={str(exc)[:300]}")
            return {"attempted": 1, "sent": 0, "already_existed": 0, "failed": 1}

        try:
            result = self.advertio_service.deliver(record, data)
            status = "already_existed" if result.get("already_existed") else "sent"
            self.repository.mark_advertio_result(record["message_id"], record["channel_username"], status=status, lead_id=result.get("lead_id"), error=None, processed_at=now)
            print(f"[ADVERTIO] {status}: message={record['message_id']} lead={result.get('lead_id')} http={result.get('http_status')}")
            return {"attempted": 1, "sent": int(status == "sent"), "already_existed": int(status == "already_existed"), "failed": 0}
        except Exception as exc:
            from delivery.advertio_client import AdvertioError
            retryable = isinstance(exc, AdvertioError) and exc.retryable
            status = "retry" if retryable else "rejected"
            self.repository.mark_advertio_result(record["message_id"], record["channel_username"], status=status, lead_id=getattr(exc, "lead_id", None), error=str(exc)[:4000], processed_at=now)
            logger.error("[ADVERTIO ERROR] status=%s retryable=%s message_id=%s detail=%s", getattr(exc, "status", None), retryable, record.get("message_id"), str(exc), exc_info=True)
            print(f"[ADVERTIO] {status}: message={record['message_id']} reason={str(exc)[:300]}")
            return {"attempted": 1, "sent": 0, "already_existed": 0, "failed": 1}

    def _process(self, records, *, progress=False):
        total = len(records)
        processed = failed = skipped = 0
        advertio = {"attempted": 0, "sent": 0, "already_existed": 0, "failed": 0}
        stopped = False
        for index, record in enumerate(records, start=1):
            source_text = self._source_text(record)
            if not source_text:
                self.repository.mark_ai_skipped(record["message_id"], record["channel_username"], reason="no_text", ai_processed_at=datetime.now(timezone.utc).isoformat())
                skipped += 1
                if progress: print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) skipped: no_text")
                continue
            self.repository.mark_ai_processing(record["message_id"], record["channel_username"])
            try:
                category, data = self._extract_with_retry(source_text, record, progress=progress)
                self.repository.save_category_record(record["id"], category, data)
                self.repository.mark_ai_result(message_id=record["message_id"], channel_username=record["channel_username"], success=True, ai_category=category, ai_processed_at=datetime.now(timezone.utc).isoformat())
                delivery = self._deliver_to_advertio(record, category, data)
                for key in advertio: advertio[key] += delivery.get(key, 0)
                processed += 1
                if progress: print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) processed -> {category}")
            except AIExtractionError as exc:
                self._log_ai_error(record, exc)
                self.repository.mark_ai_result(message_id=record["message_id"], channel_username=record["channel_username"], success=False, ai_processed_at=datetime.now(timezone.utc).isoformat())
                failed += 1
                if progress: print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) failed: {getattr(exc, 'reason', 'ai_error')}")
                if exc.stop_queue:
                    stopped = True
                    logger.error("[AI QUEUE STOPPED] provider=groq status=403 model=%s reason=%s; remaining messages stay pending", self.extractor.model, getattr(exc, "reason", "permissions_error"))
                    break
            except Exception:
                logger.exception("[AI ERROR] provider=groq model=%s reason=unexpected_error message_id=%s channel=%s", self.extractor.model, record.get("message_id"), record.get("channel_username"))
                self.repository.mark_ai_result(message_id=record["message_id"], channel_username=record["channel_username"], success=False, ai_processed_at=datetime.now(timezone.utc).isoformat())
                failed += 1
                if progress: print(f"[AI] {index}/{total} ({index * 100 / total:6.2f}%) failed: unexpected_error")
        return processed, failed, skipped, stopped, advertio

    def process_pending(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True, "advertio": None}
        records = self.repository.get_ai_pending(limit=limit, channel_username=channel_username)
        processed, failed, skipped, stopped, advertio = self._process(records)
        return {"found": len(records), "processed": processed, "failed": failed, "skipped": skipped, "stopped": stopped, "disabled": False, "advertio": advertio}

    def process_pending_with_stats(self, limit=100, channel_username=None):
        if not config.AI_EXTRACTION_ENABLED:
            print("[AI] Extraction disabled; skipping AI queue.")
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True, "advertio": None}
        records = self.repository.get_ai_pending(limit=limit, channel_username=channel_username)
        if not records:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": False, "advertio": {"attempted": 0, "sent": 0, "already_existed": 0, "failed": 0}}
        processed, failed, skipped, stopped, advertio = self._process(records, progress=True)
        return {"found": len(records), "processed": processed, "failed": failed, "skipped": skipped, "stopped": stopped, "disabled": False, "advertio": advertio}
