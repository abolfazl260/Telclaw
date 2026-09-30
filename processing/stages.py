"""Small deterministic processing stages.

No Telegram or storage dependency belongs here.  More advanced stages can be
added later without changing collection or delivery code.
"""

from .cleaner import clean_text, is_collectable_text
from .contracts import ProcessingRecord
from .normalizer import normalize_channel_username, normalize_date


class NormalizeStage:
    def process(self, record: ProcessingRecord) -> ProcessingRecord:
        data = record.data
        if "channel_username" in data:
            data["channel_username"] = normalize_channel_username(data["channel_username"])
        if "date" in data:
            data["date"] = normalize_date(data["date"])
        return record


class CleanTextStage:
    def process(self, record: ProcessingRecord) -> ProcessingRecord:
        data = record.data
        # Older messages may have NULL/empty raw_text but a populated legacy
        # text field. Prefer an original payload when available; never write it.
        source_text = data.get("raw_text") or data.get("text") or ""
        cleaned = clean_text(source_text)
        data["cleaned_text"] = cleaned
        data["text"] = cleaned
        data["is_collectable"] = is_collectable_text(cleaned)
        return record


class Pipeline:
    def __init__(self, stages):
        self.stages = list(stages)

    def process(self, record: ProcessingRecord) -> ProcessingRecord:
        for stage in self.stages:
            record = stage.process(record)
        return record
