"""SQLite lookup helpers for the official UN/LOCODE reference dataset."""

import re
import unicodedata


def _ascii(value):
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value))
    value = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "", value).upper()


def initialize(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS unlocode_locations (
            unlocode TEXT PRIMARY KEY,
            country_iso2 TEXT NOT NULL,
            location_code TEXT NOT NULL,
            name TEXT NOT NULL,
            name_ascii TEXT,
            subdivision TEXT,
            function TEXT,
            status TEXT,
            iata TEXT,
            source_version TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_unlocode_name ON unlocode_locations(country_iso2, name_ascii)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_unlocode_iata ON unlocode_locations(country_iso2, iata)"
    )


def lookup(conn, city, country_iso2):
    """Return a verified UN/LOCODE only when the local reference data has it."""
    if not city or not country_iso2:
        return None
    country = str(country_iso2).upper()
    ascii_name = _ascii(city)
    if not ascii_name:
        return None

    row = conn.execute(
        """SELECT unlocode FROM unlocode_locations
           WHERE country_iso2=? AND name_ascii=?
           ORDER BY status='AA' DESC, unlocode ASC LIMIT 1""",
        (country, ascii_name),
    ).fetchone()
    if row:
        return row[0]

    # IATA is useful for major transport locations, but is only accepted
    # when it resolves to exactly one UN/LOCODE in the requested country.
    rows = conn.execute(
        """SELECT unlocode FROM unlocode_locations
           WHERE country_iso2=? AND iata=?
           LIMIT 2""",
        (country, ascii_name),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def import_csv(conn, csv_path, source_version):
    """Import an official UN/LOCODE CSV publication into SQLite.

    Expected 12-column layout:
    Change,Country,Location,Name,NameWoDiacritics,Subdivision,
    Function,Status,Date,IATA,Coordinates,Remarks
    """
    import csv

    initialize(conn)
    count = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if len(row) < 10:
                continue
            country = row[1].strip().upper()
            location = row[2].strip().upper()
            name = row[3].strip()
            if len(country) != 2 or len(location) != 3 or not name:
                continue
            if location.startswith("."):
                continue
            unlocode = country + location
            name_ascii = row[4].strip() or _ascii(name)
            conn.execute(
                """INSERT INTO unlocode_locations(
                    unlocode, country_iso2, location_code, name, name_ascii,
                    subdivision, function, status, iata, source_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(unlocode) DO UPDATE SET
                    country_iso2=excluded.country_iso2,
                    location_code=excluded.location_code,
                    name=excluded.name,
                    name_ascii=excluded.name_ascii,
                    subdivision=excluded.subdivision,
                    function=excluded.function,
                    status=excluded.status,
                    iata=excluded.iata,
                    source_version=excluded.source_version""",
                (
                    unlocode, country, location, name, name_ascii,
                    row[5].strip(), row[6].strip(), row[7].strip(),
                    row[9].strip(), source_version,
                ),
            )
            count += 1
    conn.commit()
    return count
