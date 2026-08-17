"""Processing runner boundary for scheduled/background execution."""

from services.processing_service import ProcessingService


class ProcessingRunner:
    """Execute processing independently from scheduling and delivery."""

    def __init__(self, processing_service=None):
        self.service = processing_service or ProcessingService()

    def process(self, records):
        return self.service.process_records(records)
