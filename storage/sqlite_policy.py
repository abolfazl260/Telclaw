"""Conservative SQLite WAL policy. Change persistent mode only at startup."""
import sqlite3

BUSY_TIMEOUT_MS = 5000
AUTOCHECKPOINT_PAGES = 1000
JOURNAL_SIZE_LIMIT = 16 * 1024 * 1024


def wal_supported(version=None):
    """Require an upstream release containing the WAL-reset corruption fix.

    Vendor-specific backports cannot be inferred from sqlite_version_info.
    """
    version = tuple(version if version is not None else sqlite3.sqlite_version_info)
    return (version >= (3, 51, 3)
            or (version[:2] == (3, 50) and version >= (3, 50, 7))
            or (version[:2] == (3, 44) and version >= (3, 44, 6)))


def current_mode(conn):
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def configure_connection(conn):
    """Per-connection settings; do not renegotiate persistent journal mode."""
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    mode = current_mode(conn)
    if mode == "wal":
        if not wal_supported():
            raise RuntimeError("Unsafe SQLite runtime for an existing WAL database; "
                               "stop all writers and restart Telclaw with WAL disabled")
        conn.execute(f"PRAGMA wal_autocheckpoint={AUTOCHECKPOINT_PAGES}")
        conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    return mode


def initialize_mode(conn, *, requested):
    """Before workers start, verify or change the persistent journal mode.

    Opt-in certifies that the operator checked local filesystem compatibility.
    If WAL is disabled or the version is unsafe, an existing WAL database is
    returned to DELETE mode through SQLite itself (never remove sidecars).
    If a transition fails, fail startup instead of running an unknown mode.
    """
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=FULL")
    actual = current_mode(conn)
    eligible = wal_supported()
    desired = "wal" if requested and eligible else "delete" if actual == "wal" else actual
    if desired != actual:
        actual = str(conn.execute(f"PRAGMA journal_mode={desired}").fetchone()[0]).lower()
        if actual != desired:
            raise RuntimeError(f"SQLite journal mode transition to {desired} failed (actual={actual})")
    configure_connection(conn)
    return actual
