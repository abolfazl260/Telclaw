"""CLI entry point for AI enrichment of already processed messages."""

from storage import database
from ai.ai_service import AIProcessingService


def main():
    database.initialize_db()
    result = AIProcessingService().process_pending()
    if result.get("skipped"):
        print("[AI] disabled; set TELCLAW_AI_EXTRACTION_ENABLED=true to enable")
        return
    print(
        "[AI] found={} processed={} failed={}".format(
            result["found"], result["processed"], result["failed"]
        )
    )


if __name__ == "__main__":
    main()
