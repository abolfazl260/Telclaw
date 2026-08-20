"""Application service that orchestrates message processing."""

from datetime import datetime, timezone

from processing.classifier import ClassifierStage
from processing.contracts import ProcessingRecord
from processing.property_extractor import PropertyExtractorStage
from processing.stages import CleanTextStage, NormalizeStage, Pipeline
from storage.message_repository import MessageRepository


class ProcessingService:
    def __init__(self, stages=None, repository=None):
        self.repository = repository or MessageRepository()
        self.pipeline = Pipeline(
            stages or [NormalizeStage(), CleanTextStage(), ClassifierStage(), PropertyExtractorStage()]
        )

    def process_record(self, data):
        return self.pipeline.process(ProcessingRecord(data=dict(data))).data

    def process_records(self, records):
        return [self.process_record(record) for record in records]

    def process_pending(self, limit=500, channel_username=None):
        """Backward-compatible API returning processed records."""
        records = self.repository.get_pending(limit=limit, channel_username=channel_username)
        processed = []
        for record in records:
            try:
                result = self.process_record(record)
                self.repository.mark_processed(
                    record["message_id"], record["channel_username"],
                    text=result.get("text"), cleaned_text=result.get("cleaned_text"),
                    processing_status="processed", pipeline_version="processing-v1",
                    cleaned_at=datetime.now(timezone.utc).isoformat(),
                )
                processed.append(result)
            except Exception as exc:
                self.repository.mark_processed(
                    record["message_id"], record["channel_username"],
                    processing_status="processing_failed",
                )
                print(f"[PROCESSING] Failed message {record['message_id']}: {exc}")
        return processed

    def process_pending_with_stats(self, limit=500, channel_username=None):
        """Process one pending batch and expose terminal-friendly progress stats."""
        records = self.repository.get_pending(limit=limit, channel_username=channel_username)
        total = len(records)
        processed = 0
        failed = 0

        if not records:
            return {"found": 0, "processed": 0, "failed": 0}

        for index, record in enumerate(records, start=1):
            try:
                result = self.process_record(record)
                self.repository.mark_processed(
                    record["message_id"], record["channel_username"],
                    text=result.get("text"), cleaned_text=result.get("cleaned_text"),
                    processing_status="processed", pipeline_version="processing-v1",
                    cleaned_at=datetime.now(timezone.utc).isoformat(),
                )
                processed += 1
            except Exception as exc:
                self.repository.mark_processed(
                    record["message_id"], record["channel_username"],
                    processing_status="processing_failed",
                )
                failed += 1
                print(f"[PROCESSING] Failed message {record['message_id']}: {exc}")

            percent = index * 100 / total
            print(
                f"[PROCESSING] {index}/{total} ({percent:6.2f}%) "
                f"processed={processed} failed={failed}"
            )

        return {"found": total, "processed": processed, "failed": failed}
