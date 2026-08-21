"""CLI entry point for the complete processing and AI enrichment pipeline."""

from storage import database
from processing.processing_service import ProcessingService
from ai.ai_service import AIProcessingService


def main():
    database.initialize_db()
    result = ProcessingService().process_pending()
    print(
        "[PROCESSING] found={} processed={} failed={}".format(
            result["found"], result["processed"], result["failed"]
        )
    )

    ai_result = AIProcessingService().process_pending()
    if ai_result.get("skipped"):
        print("[AI] disabled; processed messages remain available for later AI enrichment")
        return
    print(
        "[AI] found={} processed={} failed={}".format(
            ai_result["found"], ai_result["processed"], ai_result["failed"]
        )
    )


if __name__ == "__main__":
    main()
