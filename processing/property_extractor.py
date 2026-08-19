"""Property extraction extension point.

The stage intentionally does not implement provider-specific or AI logic yet.
It creates a stable place for the future Property Information schema.
"""

from .contracts import ProcessingRecord


class PropertyExtractorStage:
    """Prepare a record for future property-field extraction."""

    def process(self, record: ProcessingRecord) -> ProcessingRecord:
        record.data.setdefault("property", {})
        record.data.setdefault("property_extraction_status", "pending")
        return record
