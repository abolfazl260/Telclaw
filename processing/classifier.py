"""Replaceable deterministic message classification stage."""

from .contracts import ProcessingRecord


class ClassifierStage:
    """Classify records without coupling the pipeline to an AI provider."""

    def process(self, record: ProcessingRecord) -> ProcessingRecord:
        text = record.data.get("cleaned_text", "")
        record.data.setdefault("classification", "unclassified")
        record.data["classification_text_length"] = len(text)
        return record
