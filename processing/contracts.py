"""Contracts shared by the processing pipeline."""

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol


@dataclass
class ProcessingRecord:
    """A message plus fields produced by processing stages."""

    data: Dict[str, Any] = field(default_factory=dict)


class Processor(Protocol):
    def process(self, record: ProcessingRecord) -> ProcessingRecord:
        """Transform a record and return the same logical record."""
