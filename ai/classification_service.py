"""Independent batch service for AI category classification."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import random
import re
import time

import config
from ai.provider_manager import AIProviderManager
from ai.text_source import select_source_text
from ai.extractor import AIExtractionError
from storage.message_repository import MessageRepository

logger = logging.getLogger("telclaw.ai.classification")


class CategoryClassificationService:
    """Move cleaned messages from classification queue to extraction queue."""

    def __init__(self, repository=None, provider_manager=None, classifier=None, batch_size=None):
        self.repository = repository or MessageRepository()
        # classifier remains an injection alias for backwards-compatible callers.
        self.provider_manager = provider_manager or AIProviderManager(provider=classifier)
        # An explicitly injected batch size remains fixed for tests/special callers.
        # Normal runtime instances resolve the current config value on every run so
        # Back Office changes take effect without rebuilding the service.
        self.batch_size = int(batch_size) if batch_size is not None else None

    def _effective_batch_size(self, limit=None):
        return max(1, int(
            limit
            if limit is not None
            else (self.batch_size if self.batch_size is not None else config.AI_CLASSIFICATION_BATCH_SIZE)
        ))

    @staticmethod
    def _source_text(record):
        return select_source_text(record)

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

    def _process_all_pending(self, limit=None, channel_username=None, should_stop=None, *, progress=False):
        batch_size = self._effective_batch_size(limit)
        total_found = processed = failed = skipped = 0
        stopped = False
        batch_number = 0

        while True:
            if should_stop and should_stop():
                stopped = True
                break

            records = self.repository.get_classification_pending(
                limit=batch_size,
                channel_username=channel_username,
            )
            if not records:
                break

            batch_number += 1
            if progress:
                print(
                    f"[AI CLASSIFICATION QUEUE] batch={batch_number} "
                    f"size={len(records)} batch_size={batch_size}"
                )

            stats = self._process_batch(
                records,
                progress=progress,
                should_stop=should_stop,
            )
            total_found += len(records)
            processed += int(stats.get("processed") or 0)
            failed += int(stats.get("failed") or 0)
            skipped += int(stats.get("skipped") or 0)

            if stats.get("stopped"):
                stopped = True
                break

            # A failed classification remains retryable until its retry limit is
            # exhausted. Do not immediately pick the same failed rows again in
            # this cycle; defer retries to the next scheduler cycle to avoid
            # hammering an unhealthy provider and burning all retries at once.
            if stats.get("failed"):
                break

        remaining = len(self.repository.get_classification_pending(
            limit=batch_size,
            channel_username=channel_username,
        ))
        if progress:
            print(
                f"[AI CLASSIFICATION QUEUE] finished | found={total_found} "
                f"processed={processed} skipped={skipped} failed={failed} "
                f"remaining={remaining} stopped={stopped}"
            )
        return {
            "found": total_found,
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "stopped": stopped,
            "disabled": False,
            "remaining": remaining,
        }

    def process_pending(self, limit=None, channel_username=None, should_stop=None):
        if not config.AI_CLASSIFICATION_ENABLED:
            return {"found": 0, "processed": 0, "failed": 0, "skipped": 0, "stopped": False, "disabled": True}
        stats = self._process_all_pending(
            limit=limit,
            channel_username=channel_username,
            should_stop=should_stop,
            progress=False,
        )
        # Preserve the historical process_pending() response shape for callers
        # that compare the result exactly.
        stats.pop("remaining", None)
        return stats

    def process_pending_with_stats(self, limit=None, channel_username=None, should_stop=None):
        if not config.AI_CLASSIFICATION_ENABLED:
            print("[AI CLASSIFICATION] disabled; skipping classification queue.")
            return {
                "found": 0,
                "processed": 0,
                "failed": 0,
                "skipped": 0,
                "stopped": False,
                "disabled": True,
                "remaining": 0,
            }
        return self._process_all_pending(
            limit=limit,
            channel_username=channel_username,
            should_stop=should_stop,
            progress=True,
        )


__all__ = ["CategoryClassificationService"]
