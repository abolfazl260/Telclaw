"""Application service that orchestrates message processing."""

from processing.contracts import ProcessingRecord
from processing.stages import CleanTextStage, NormalizeStage, Pipeline


class ProcessingService:
    """Run the processing pipeline without knowing its infrastructure."""

    def __init__(self, stages=None):
        self.pipeline = Pipeline(
            stages or [NormalizeStage(), CleanTextStage()]
        )

    def process_record(self, data):
        record = ProcessingRecord(data=dict(data))
        return self.pipeline.process(record).data

    def process_records(self, records):
        return [self.process_record(record) for record in records]
