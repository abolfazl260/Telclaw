"""Import an official UNECE UN/LOCODE CSV publication into Telclaw SQLite.

Usage:
    python3 scripts/import_unlocode.py /path/to/locode_csv_dir --version 2025-1

The directory should contain the official CodeListPart CSV files. The importer
intentionally requires local source files. It never invents or guesses codes.
"""

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import database
from storage.unlocode_repository import import_csv


def main():
    parser = argparse.ArgumentParser(description="Import official UN/LOCODE CSV into Telclaw SQLite")
    parser.add_argument("csv_path", help="Official UN/LOCODE CSV file or directory containing CodeListPart CSV files")
    parser.add_argument("--version", required=True, help="UN/LOCODE release, for example 2025-1")
    args = parser.parse_args()

    if os.path.isdir(args.csv_path):
        paths = sorted(glob.glob(os.path.join(args.csv_path, "*UNLOCODE*CodeListPart*.csv")))
    else:
        paths = [args.csv_path] if os.path.isfile(args.csv_path) else []

    if not paths:
        raise SystemExit(f"No UN/LOCODE CodeListPart CSV files found: {args.csv_path}")

    database.initialize_db()
    conn = database.get_connection()
    total = 0
    try:
        for path in paths:
            count = import_csv(conn, path, args.version)
            total += count
            print(f"Imported {count:>6} locations from {os.path.basename(path)}")
    finally:
        conn.close()

    print(f"Total imported: {total} | version={args.version}")


if __name__ == "__main__":
    main()
