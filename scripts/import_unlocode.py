"""Import an official UNECE UN/LOCODE CSV publication into Telclaw SQLite.

Usage:
    python3 scripts/import_unlocode.py /path/to/UNLOCODE-CodeList.csv --version 2025-1

The importer intentionally requires a local CSV. It never invents or guesses
UN/LOCODE values and stores the release version with every imported row.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import database
from storage.unlocode_repository import import_csv


def main():
    parser = argparse.ArgumentParser(description="Import official UN/LOCODE CSV into Telclaw SQLite")
    parser.add_argument("csv_path", help="Path to an official UN/LOCODE CSV file")
    parser.add_argument("--version", required=True, help="UN/LOCODE release, for example 2025-1")
    args = parser.parse_args()

    if not os.path.isfile(args.csv_path):
        raise SystemExit(f"CSV file not found: {args.csv_path}")

    database.initialize_db()
    conn = database.get_connection()
    try:
        count = import_csv(conn, args.csv_path, args.version)
    finally:
        conn.close()

    print(f"Imported {count} UN/LOCODE locations | version={args.version}")


if __name__ == "__main__":
    main()
