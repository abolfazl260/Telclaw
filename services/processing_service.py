"""Application service that orchestrates message processing."""

from processing.classifier import ClassifierStage
from processing.contracts import ProcessingRecord
from processing.property_extractor import PropertyExtractorStage
from processing.stages import CleanTextStage, NormalizeStage, Pipeline


class ProcessingService:
    """Run processing stages without knowing their infrastructure."""

    def __init__(self, stages=None):
        self.pipeline = Pipeline(
            stages
            or [
                NormalizeStage(),
                CleanTextStage(),
                ClassifierStage(),
                PropertyExtractorStage(),
            ]
        )

    def process_record(self, data):
        record = ProcessingRecord(data=dict(data))
        return self.pipeline.process(record).data

    def process_records(self, records):
        return [self.process_record(record) for record in records]
