"""Orchestrates provider-agnostic extraction from the independent AI queue."""

from datetime import datetime, timezone
import logging
import random
import re
import time

import config
from ai.extractor import AIExtractionError
from ai.provider_manager import AIProviderManager
from storage.message_repository import MessageRepository
from services.stage_control import get_stage_control

logger = logging.getLogger("telclaw.ai")


class AIProcessingService:
    def __init__(self, repository=None, provider_manager=None, extractor=None, advertio_service=None, media_downloader=None):
        self.repository = repository or MessageRepository()
        self.provider_manager = provider_manager or AIProviderManager(provider=extractor)
        self.advertio_service = advertio_service
        self.media_downloader = media_downloader
        if self.advertio_service is None and config.ADVERTIO_INGEST_ENABLED:
            from delivery.advertio_service import AdvertioDeliveryService
            self.advertio_service = AdvertioDeliveryService()
        self._print_provider_status(prefix="[AI QUEUE]")

    def _print_provider_status(self, prefix="[AI]"):
        print(
            f"{prefix} provider={getattr(self.provider_manager, 'provider', 'unknown')} | "
            f"model={getattr(self.provider_manager, 'model', None)}"
        )

    def _print_message_status(self, index, total, record, category, status, **extra):
        provider = getattr(self.provider_manager, "provider", "unknown")
        model = getattr(self.provider_manager, "model", None)
        details = " | ".join(f"{key}={value}" for key, value in extra.items())
        suffix = f" | {details}" if details else ""
        print(
            f"[AI] {index}/{total} | message={record.get('message_id')} | "
            f"provider={provider} | model={model} | category={category or '-'} | "
            f"status={status}{suffix}"
        )

    def set_media_downloader(self, media_downloader):
        self.media_downloader = media_downloader

    @staticmethod
    def _source_text(record):
        return (record.get("cleaned_text") or record.get("text") or record.get("raw_text") or "").strip()

    @staticmethod
    def _classification_category(record):
        return str(record.get("classification_category") or "").strip().lower()

    def _log_ai_error(self, record, exc):
        logger.error(
            "[AI ERROR] provider=%s status=%s model=%s reason=%s message_id=%s channel=%s detail=%s",
            getattr(exc, "provider", getattr(self.provider_manager, "provider", "groq-1")),
            getattr(exc, "status", None),
            getattr(self.provider_manager, "model", None),
            getattr(exc, "reason", None),
            record.get("message_id"),
            record.get("channel_username"),
            str(exc),
            exc_info=True,
        )

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
        logger.warning("[AI RATE LIMIT] provider=%s model=%s status=429 retry=%s/%s wait=%.2fs reason=%s", getattr(self.provider_manager, "provider", "groq-1"), getattr(self.provider_manager, "model", None), retry_number, config.GROQ_RATE_LIMIT_MAX_RETRIES, wait, reason)
        print(f"[AI] rate_limit_wait | provider={getattr(self.provider_manager, 'provider', 'groq-1')} | model={getattr(self.provider_manager, 'model', None)} | retry={retry_number}/{config.GROQ_RATE_LIMIT_MAX_RETRIES} | wait={wait:.2f}s | reason={reason}")
        time.sleep(wait)

    def _extract_with_retry(self, source_text, record, category, progress=False):
        rate_limit_attempts = 0
        invalid_json_attempts = 0
        while True:
            try:
                self._print_message_status(0, 0, record, category, "request_started") if progress else None
                result = self.provider_manager.extract(source_text, category)
                if progress:
                    self._print_message_status(0, 0, record, category, "request_completed")
                return result
            except AIExtractionError as exc:
                if getattr(exc, "reason", None) == "invalid_provider_output":
                    if invalid_json_attempts >= config.GROQ_INVALID_JSON_MAX_RETRIES:
                        raise
                    invalid_json_attempts += 1
                    logger.warning(
                        "[AI INVALID PROVIDER OUTPUT] provider=%s model=%s retry=%s/%s message_id=%s channel=%s",
                        getattr(self.provider_manager, "provider", "groq-1"),
                        getattr(self.provider_manager, "model", None),
                        invalid_json_attempts,
                        config.GROQ_INVALID_JSON_MAX_RETRIES,
                        record.get("message_id"),
                        record.get("channel_username"),
                    )
                    if progress:
                        self._print_message_status(0, 0, record, category, "invalid_provider_output_retry", retry=f"{invalid_json_attempts}/{config.GROQ_INVALID_JSON_MAX_RETRIES}")
                    continue
                if getattr(exc, "status", None) != 429 or rate_limit_attempts >= config.GROQ_RATE_LIMIT_MAX_RETRIES:
                    raise
                rate_limit_attempts += 1
                if progress:
                    self._print_message_status(0, 0, record, category, "rate_limit_retry", retry=f"{rate_limit_attempts}/{config.GROQ_RATE_LIMIT_MAX_RETRIES}")
                self._rate_limit_wait(exc, rate_limit_attempts)

    def _prepare_media_for_advertio(self, record):
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
            return {"attempted": 0, "sent": 0, "already_existed": 0, "failed": 0, "skipped": 0}
        if get_stage_control().is_skip_requested("advertio"):
            print(f"[ADVERTIO] Stage skip requested; remaining delivery stays pending. message={record['message_id']}")
            return {"attempted": 0, "sent": 0, "already_existed": 0, "failed": 0, "skipped": 1}
        now = datetime.now(timezone.utc).isoformat()
        try:
            print(f"[MEDIA] message={record['message_id']} status=preparing")
            self._prepare_media_for_advertio(record)
        except Exception as exc:
            self.repository.mark_advertio_result(record["message_id"], record["channel_username"], status="retry", lead_id=None, error=str(exc)[:4000], processed_at=now)
            logger.error("[MEDIA ERROR] message_id=%s channel=%s detail=%s", record.get("message_id"), record.get("channel_username"), str(exc), exc_info=True)
            print(f"[MEDIA] retry: message={record['message_id']} reason={str(exc)[:300]}")
            return {"attempted": 1, "sent": 0, "already_existed": 0, "failed": 1, "skipped": 0}
        try:
            print(f"[ADVERTIO] message={record['message_id']} status=request_started")
            result = self.advertio_service.deliver(record, data)
            status = "already_existed" if result.get("already_existed") else "sent"
            self.repository.mark_advertio_result(record["message_id"], record["channel_username"], status=status, lead_id=result.get("lead_id"), error=None, processed_at=now)
            print(f"[ADVERTIO] {status}: message={record['message_id']} lead={result.get('lead_id')} http={result.get('http_status')}")
            return {"attempted": 1, "sent": int(status == "sent"), "already_existed": int(status == "already_existed"), "failed": 0, "skipped": 0}
        except Exception as exc:
            from delivery.advertio_client import AdvertioError
            retryable = isinstance(exc, AdvertioError) and exc.retryable
            status = "retry" if retryable else "rejected"
            self.repository.mark_advertio_result(record["message_id"], record["channel_username"], status=status, lead_id=getattr(exc, "lead_id", None), error=str(exc)[:4000], processed_at=now)
            logger.error("[ADVERTIO ERROR] status=%s retryable=%s message_id=%s detail=%s", getattr(exc, "status", None), retryable, record.get("message_id"), str(exc), exc_info=True)
            print(f"[ADVERTIO] {status}: message={record['message_id']} reason={str(exc)[:300]}")
            return {"attempted": 1, "sent": 0, "already_existed": 0, "failed": 1, "skipped": 0}

    def _process(self, records, *, progress=False, should_stop=None, offset=0):
        total = len(records)
        processed = failed = skipped = 0
        advertio = {"attempted": 0, "sent": 0, "already_existed": 0, "failed": 0, "skipped": 0}
        stopped = False
        for local_index, record in enumerate(records, start=1):
            index = offset + local_index
            category = self._classification_category(record)
            if should_stop and should_stop():
                stopped = True
                print(f"[AI] Stage skip requested; remaining messages stay pending. next_message={record.get('message_id')}")
                break
            self._print_message_status(index, offset + total, record, category, "processing") if progress else None
            source_text = self._source_text(record)
            if category not in {"housinglist", "transferlist", "joblist"}:
                self.repository.mark_ai_skipped(record["message_id"], record["channel_username"], reason="invalid_classification_category", ai_processed_at=datetime.now(timezone.utc).isoformat())
                skipped += 1
                if progress: self._print_message_status(index, offset + total, record, category, "skipped", reason="invalid_classification_category")
                continue
            if not config.is_ai_extraction_enabled(category):
                self.repository.mark_ai_skipped(record["message_id"], record["channel_username"], reason=f"category_disabled:{category}", ai_processed_at=datetime.now(timezone.utc).isoformat())
                skipped += 1
                if progress: self._print_message_status(index, offset + total, record, category, "skipped", reason="category_disabled")
                continue
            if not source_text:
                self.repository.mark_ai_skipped(record["message_id"], record["channel_username"], reason="no_text", ai_processed_at=datetime.now(timezone.utc).isoformat())
                skipped += 1
                if progress: self._print_message_status(index, offset + total, record, category, "skipped", reason="no_text")
                continue
            logger.info("AI Classification: %s | AI Extraction: processing %s", category, category)
            self.repository.mark_ai_processing(record["message_id"], record["channel_username"])
            try:
                result = self._extract_with_retry(source_text, record, category, progress=False)
                if result.get("category") != category:
                    raise AIExtractionError(f"Extraction category mismatch: expected={category} received={result.get('category')}", reason="category_mismatch")
                data = result.get("data", {}).get(category)
                if not isinstance(data, dict):
                    raise AIExtractionError(f"Missing extracted data for classified category: {category}", reason="invalid_provider_output")
                self.repository.save_category_record(record["id"], category, data)
                self.repository.mark_ai_result(message_id=record["message_id"], channel_username=record["channel_username"], success=True, ai_category=category, ai_processed_at=datetime.now(timezone.utc).isoformat())
                delivery = self._deliver_to_advertio(record, category, data)
                for key in advertio: advertio[key] += delivery.get(key, 0)
                processed += 1
                if progress: self._print_message_status(index, offset + total, record, category, "processed", advertio="ok" if not delivery.get("failed") else "failed")
            except AIExtractionError as exc:
                self._log_ai_error(record, exc)
                self.repository.mark_ai_result(message_id=record["message_id"], channel_username=record["channel_username"], success=False, ai_processed_at=datetime.now(timezone.utc).isoformat())
                failed += 1
                if progress: self._print_message_status(index, offset + total, record, category, "failed", reason=getattr(exc, "reason", "ai_error"))
                if exc.stop_queue:
                    stopped = True
                    logger.error("[AI QUEUE STOPPED] provider=%s status=403 model=%s reason=%s; remaining messages stay pending", getattr(self.provider_manager, "provider", "groq-1"), self.provider_manager.model, getattr(exc, "reason", "permissions_error"))
                    if progress: print(f"[AI] QUEUE STOPPED | message={record.get('message_id')} | provider={getattr(self.provider_manager, 'provider', 'unknown')} | model={getattr(self.provider_manager, 'model', None)} | reason={getattr(exc, 'reason', 'permissions_error')}")
                    break
            except Exception as exc:
                logger.exception("[AI ERROR] provider=%s model=%s reason=unexpected_error message_id=%s channel=%s", getattr(self.provider_manager, "provider", "groq-1"), self.provider_manager.model, record.get("message_id"), record.get("channel_username"))
                self.repository.mark_ai_result(message_id=record["message_id"], channel_username=record["channel_username"], success=False, ai_processed_at=datetime.now(timezone.utc).isoformat())
                failed += 1
                if progress: self._print_message_status(index, offset + total, record, category, "failed", reason="unexpected_error", detail=str(exc)[:200])
        return processed, failed, skipped, stopped, advertio

    def process_pending(self, limit=100, channel_username=None, should_stop=None):
        if not config.AI_EXTRACTION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True, "advertio": None}
        return self._process_all_pending(limit=limit, channel_username=channel_username, should_stop=should_stop, progress=False)

    def process_pending_with_stats(self, limit=100, channel_username=None, should_stop=None):
        if not config.AI_EXTRACTION_ENABLED:
            print("[AI] Extraction disabled; skipping AI queue.")
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True, "advertio": None, "remaining": 0}
        return self._process_all_pending(limit=limit, channel_username=channel_username, should_stop=should_stop, progress=True)

    def _process_all_pending(self, limit, channel_username, should_stop, progress):
        total_found = processed = failed = skipped = 0
        stopped = False
        advertio = {"attempted": 0, "sent": 0, "already_existed": 0, "failed": 0, "skipped": 0}
        batch_number = 0
        while True:
            if should_stop and should_stop():
                stopped = True
                break
            records = self.repository.get_ai_pending(limit=limit, channel_username=channel_username)
            if not records:
                break
            batch_number += 1
            if progress:
                if batch_number > 1:
                    print(f"[AI QUEUE] fetching next batch={batch_number} | batch_size={len(records)}")
                else:
                    print(f"[AI QUEUE] started | pending_snapshot={len(records)} | batch_size={limit}")
                self._print_provider_status()
            batch_processed, batch_failed, batch_skipped, batch_stopped, batch_advertio = self._process(
                records,
                progress=progress,
                should_stop=should_stop,
                offset=total_found,
            )
            processed += batch_processed
            failed += batch_failed
            skipped += batch_skipped
            total_found += len(records)
            for key in advertio:
                advertio[key] += batch_advertio.get(key, 0)
            if batch_stopped:
                stopped = True
                break
            if len(records) < limit:
                # This is only an optimization; the database is still queried once more
                # so newly-visible pending records are not missed.
                continue
        remaining = len(self.repository.get_ai_pending(limit=limit, channel_username=channel_username))
        if progress:
            print(
                f"[AI QUEUE] finished | found={total_found} | processed={processed} | "
                f"failed={failed} | skipped={skipped} | remaining={remaining} | stopped={stopped}"
            )
            self._print_provider_status(prefix="[AI QUEUE] final_provider")
        return {
            "found": total_found,
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "stopped": stopped,
            "disabled": False,
            "advertio": advertio,
            "remaining": remaining,
        }
