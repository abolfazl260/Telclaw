"""CLI entry point for processing collected SQLite messages."""

from storage import database
from processing.processing_service import ProcessingService


def main():
    database.initialize_db()
    result = ProcessingService().process_pending()
    print(
        f"[PROCESSING] found={result['found']} "
        f"processed={result['processed']} failed={result['failed']}"
    )


if __name__ == "__main__":
    main()
