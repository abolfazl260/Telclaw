"""Application service that runs the independent processing queue."""

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

    def _process(self, records, *, progress=False, should_stop=None):
        total = len(records)
        processed = failed = 0
        stopped = False
        for index, record in enumerate(records, start=1):
            if should_stop and should_stop():
                stopped = True
                print(f"[PROCESSING] Stage skip requested; remaining {total - index + 1} records stay pending.")
                break
            self.repository.mark_processing(
                record["message_id"], record["channel_username"]
            )
            try:
                result = self.process_record(record)
                self.repository.mark_processing_result(
                    record["message_id"], record["channel_username"],
                    success=True,
                    text=result.get("text"),
                    cleaned_text=result.get("cleaned_text"),
                    pipeline_version="processing-v1",
                    cleaned_at=datetime.now(timezone.utc).isoformat(),
                )
                processed += 1
            except Exception as exc:
                self.repository.mark_processing_result(
                    record["message_id"], record["channel_username"],
                    success=False,
                )
                failed += 1
                print(f"[PROCESSING][FAILED] message_id={record['message_id']} channel={record['channel_username']} reason=processing_exception detail={exc}")

            if progress:
                percent = index * 100 / total
                print(
                    f"[PROCESSING] {index}/{total} ({percent:6.2f}%) "
                    f"processed={processed} failed={failed}"
                )
        return processed, failed, stopped

    def process_pending(self, limit=500, channel_username=None, should_stop=None):
        """Process only records waiting in the processing queue."""
        records = self.repository.get_processing_pending(
            limit=limit, channel_username=channel_username
        )
        processed, _, _ = self._process(records, should_stop=should_stop)
        return processed

    def process_pending_with_stats(self, limit=500, channel_username=None, should_stop=None):
        """Process one independent queue batch and expose progress stats."""
        records = self.repository.get_processing_pending(
            limit=limit, channel_username=channel_username
        )
        if not records:
            return {"found": 0, "processed": 0, "failed": 0, "stopped": False}
        processed, failed, stopped = self._process(records, progress=True, should_stop=should_stop)
        return {"found": len(records), "processed": processed, "failed": failed, "stopped": stopped}
