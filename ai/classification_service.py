"""Independent batch service for AI category classification."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import random
import re
import time

import config
from ai.provider_manager import AIProviderManager
from ai.extractor import AIExtractionError
from storage.message_repository import MessageRepository

logger = logging.getLogger("telclaw.ai.classification")


class CategoryClassificationService:
    """Move cleaned messages from classification queue to extraction queue."""

    def __init__(self, repository=None, provider_manager=None, classifier=None, batch_size=None):
        self.repository = repository or MessageRepository()
        # classifier remains an injection alias for backwards-compatible callers.
        self.provider_manager = provider_manager or AIProviderManager(provider=classifier)
        self.batch_size = int(batch_size or config.AI_CLASSIFICATION_BATCH_SIZE)

    @staticmethod
    def _source_text(record):
        return (record.get("cleaned_text") or record.get("text") or record.get("raw_text") or "").strip()

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
            wait = max(0.0, float(provider_wait))
        else:
            base = min(2 ** retry_number, config.GROQ_RATE_LIMIT_MAX_WAIT_SECONDS)
            wait = max(config.GROQ_RATE_LIMIT_MIN_WAIT_SECONDS, min(base + random.uniform(0, min(1.0, base * 0.25)), config.GROQ_RATE_LIMIT_MAX_WAIT_SECONDS))
        logger.warning("[AI CLASSIFICATION RATE LIMIT] retry=%s/%s wait=%.2fs", retry_number, config.GROQ_RATE_LIMIT_MAX_RETRIES, wait)
        print(f"[AI CLASSIFICATION] rate limit retry={retry_number}/{config.GROQ_RATE_LIMIT_MAX_RETRIES} wait={wait:.2f}s")
        time.sleep(wait)

    def _classify_with_retry(self, batch):
        rate_limit_attempts = 0
        invalid_json_attempts = 0
        while True:
            try:
                return self.provider_manager.classify_batch(batch)
            except AIExtractionError as exc:
                if getattr(exc, "reason", None) == "invalid_provider_output":
                    if invalid_json_attempts >= config.GROQ_INVALID_JSON_MAX_RETRIES:
                        raise
                    invalid_json_attempts += 1
                    logger.warning("[AI CLASSIFICATION INVALID JSON] retry=%s/%s", invalid_json_attempts, config.GROQ_INVALID_JSON_MAX_RETRIES)
                    continue
                if getattr(exc, "status", None) != 429 or rate_limit_attempts >= config.GROQ_RATE_LIMIT_MAX_RETRIES:
                    raise
                rate_limit_attempts += 1
                self._rate_limit_wait(exc, rate_limit_attempts)

    def _mark_no_text(self, record):
        self.repository.mark_classification_result(
            record["message_id"],
            record["channel_username"],
            category="none",
            success=True,
            processed_at=datetime.now(timezone.utc).isoformat(),
            attempts=int(record.get("classification_attempts") or 0) + 1,
        )

    @staticmethod
    def _report_error(message, *args, exc_info=False):
        """Write classification failures to the application log and terminal."""
        logger.error(message, *args, exc_info=exc_info)
        try:
            rendered = message % args if args else message
        except (TypeError, ValueError):
            rendered = message
        print(rendered)

    def _mark_batch_failed(self, candidates, exc, skipped):
        now = datetime.now(timezone.utc).isoformat()
        error = str(exc)[:4000]
        for item in candidates:
            record = item["record"]
            self.repository.mark_classification_result(
                record["message_id"],
                record["channel_username"],
                success=False,
                error=error,
                processed_at=now,
                attempts=int(record.get("classification_attempts") or 0) + 1,
            )
        self._report_error(
            "[AI CLASSIFICATION ERROR] failed whole batch: %s",
            exc,
            exc_info=True,
        )
        return {"processed": 0, "failed": len(candidates), "skipped": skipped, "stopped": bool(getattr(exc, "stop_queue", False))}

    def _process_batch(self, records, *, progress=False, should_stop=None):
        if should_stop and should_stop():
            return {"processed": 0, "failed": 0, "skipped": 0, "stopped": True}

        candidates = []
        skipped = 0
        for record in records:
            source_text = self._source_text(record)
            if not source_text:
                self._mark_no_text(record)
                skipped += 1
                continue
            self.repository.mark_classification_processing(record["message_id"], record["channel_username"])
            candidates.append({"message_id": record["message_id"], "text": source_text, "record": record})

        if not candidates:
            return {"processed": 0, "failed": 0, "skipped": skipped, "stopped": False}

        try:
            classifications = self._classify_with_retry(candidates)
        except AIExtractionError as exc:
            return self._mark_batch_failed(candidates, exc, skipped)
        except Exception as exc:
            return self._mark_batch_failed(candidates, exc, skipped)

        processed = failed = 0
        now = datetime.now(timezone.utc).isoformat()
        for item in candidates:
            record = item["record"]
            category = classifications.get(int(record["message_id"]))
            if category:
                self.repository.mark_classification_result(
                    record["message_id"],
                    record["channel_username"],
                    category=category,
                    success=True,
                    processed_at=now,
                    attempts=int(record.get("classification_attempts") or 0) + 1,
                )
                processed += 1
                if progress:
                    print(f"[AI CLASSIFICATION] message={record['message_id']} -> {category}")
            else:
                self.repository.mark_classification_result(
                    record["message_id"],
                    record["channel_username"],
                    success=False,
                    error="missing classification result",
                    processed_at=now,
                    attempts=int(record.get("classification_attempts") or 0) + 1,
                )
                self._report_error(
                    "[AI CLASSIFICATION ERROR] message=%s channel=%s: missing classification result",
                    record["message_id"],
                    record["channel_username"],
                )
                failed += 1
        return {"processed": processed, "failed": failed, "skipped": skipped, "stopped": False}

    def process_pending(self, limit=None, channel_username=None, should_stop=None):
        if not config.AI_CLASSIFICATION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True}
        limit = int(limit or self.batch_size)
        records = self.repository.get_classification_pending(limit=limit, channel_username=channel_username)
        stats = self._process_batch(records, should_stop=should_stop)
        return {"found": len(records), **stats, "disabled": False}

    def process_pending_with_stats(self, limit=None, channel_username=None, should_stop=None):
        if not config.AI_CLASSIFICATION_ENABLED:
            print("[AI CLASSIFICATION] disabled; skipping classification queue.")
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True}
        limit = int(limit or self.batch_size)
        records = self.repository.get_classification_pending(limit=limit, channel_username=channel_username)
        if not records:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": False}
        stats = self._process_batch(records, progress=True, should_stop=should_stop)
        return {"found": len(records), **stats, "disabled": False}


__all__ = ["CategoryClassificationService"]
