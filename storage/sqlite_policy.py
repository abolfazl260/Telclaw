"""Conservative SQLite WAL policy. Change persistent mode only at startup."""
import logging
from pathlib import Path
import re
import sqlite3

LOG = logging.getLogger(__name__)
# Conservative Linux allowlist; unknown, network, FUSE and overlay mounts
# remain in rollback mode. Filesystem type is not proof of host exclusivity.
LOCAL_FILESYSTEMS = frozenset({'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'f2fs', 'zfs'})
ROLLBACK_MODES = frozenset({'delete', 'truncate', 'persist'})

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


def filesystem_type(db_path, mountinfo=None):
    """Resolve the deepest Linux mount for the actual database path.

    Decode kernel mountinfo octal escapes and fail closed on unknown mounts.
    Other platforms intentionally fall back to rollback mode.
    """
    try:
        path = Path(db_path).resolve()
        if mountinfo is None:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        best = None
        for line in mountinfo.splitlines():
            left, right = line.split(" - ", 1)
            fields = left.split()
            mount = Path(re.sub(r"\\([0-7]{3})",
                                lambda m: chr(int(m.group(1), 8)), fields[4]))
            if path == mount or mount in path.parents:
                candidate = (len(mount.parts), right.split()[0])
                if best is None or candidate[0] >= best[0]:
                    best = candidate
        return best[1] if best else "unknown"
    except (OSError, ValueError, IndexError, RuntimeError):
        return "unknown"


def eligibility(db_path):
    fs = filesystem_type(db_path)
    if not wal_supported():
        return fs, "unsupported_sqlite_version"
    if fs not in LOCAL_FILESYSTEMS:
        return fs, "unsupported_or_unknown_filesystem"
    return fs, "eligible"


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
                               "stop all writers and restart Telclaw to restore rollback mode")
        conn.execute(f"PRAGMA wal_autocheckpoint={AUTOCHECKPOINT_PAGES}")
        conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    return mode


def initialize_mode(conn, *, db_path):
    """Select mode automatically before workers start.

    A failed WAL activation is nonfatal only when rollback mode is verified.
    An unsafe existing WAL must be converted successfully or startup stops.
    Never delete sidecars or hide database errors.
    """
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=FULL")
    actual = current_mode(conn)
    fs, reason = eligibility(db_path)
    desired = "wal" if reason == "eligible" else (
        actual if actual in ROLLBACK_MODES else "delete")
    if desired != actual:
        try:
            actual = str(conn.execute(f"PRAGMA journal_mode={desired}").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            if desired != "wal":
                raise
            actual = current_mode(conn)
            if actual not in ROLLBACK_MODES:
                raise
            reason = "wal_activation_failed"
            LOG.warning("WAL activation failed; verified rollback mode %s: %s", actual, exc)
        if actual != desired:
            if desired == "wal" and actual in ROLLBACK_MODES:
                reason = "wal_activation_failed"
            else:
                raise RuntimeError(f"SQLite journal mode transition to {desired} failed (actual={actual})")
    configure_connection(conn)
    if actual != "wal":
        LOG.warning("SQLite using %s instead of WAL: %s (filesystem=%s)", actual, reason, fs)
    return actual
