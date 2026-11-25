# SQLite WAL rollout

WAL allows readers and a writer to coexist; SQLite still serializes writers.
No schema or data migration is needed. This change does not shrink the DB.

## Preflight (production operator)

1. Check the SQLite version linked to the **service's Python interpreter**:
   `python -c "import sqlite3; print(sqlite3.sqlite_version)"`.
   Upstream WAL-reset fix: >=3.51.3, 3.50.x >=3.50.7, or 3.44.x >=3.44.6.
   See https://sqlite.org/wal.html#the_wal_reset_bug . Unknown vendor
   backports are intentionally not assumed safe.
2. Confirm the database and sidecars live on a local filesystem supporting
   SQLite file locks and shared memory (not NFS/SMB or a multi-host mount).
   On Linux, inspect `findmnt -T /absolute/path/to/telclaw.db`. All users of the
   DB must run compatible SQLite builds. Review external backup scripts.
3. Create a verified backup via SQLite's backup API (e.g. Telegram /database).
   Stop Telclaw and all other DB readers/writers before changing journal mode.
4. Set `TELCLAW_SQLITE_WAL_ENABLED=true` in the actual service .env. Default
   is false because filesystem/runtime compatibility has not been verified.
   Restart the service. Check startup [SQLITE] output and Health > Database
   for the actual WAL mode and version. Unsupported versions remain in
   rollback mode; no attempt is made to enable WAL on them.

Startup switches mode before schema migration/workers. Normal connections
do not renegotiate it. Existing WAL is converted to DELETE when disabled
or unsupported, using SQLite, preserving committed data. If a mode change
fails, startup stops rather than silently operating in an uncertain mode.
Other standalone entrypoints do not activate WAL; they refuse unsafe WAL
connections through the shared database helper.

## Connection settings

- synchronous=FULL; foreign_keys=ON; busy_timeout=5000 ms.
- WAL autocheckpoint 1000 pages and journal_size_limit 16 MiB on each
  WAL connection. The latter is a retained-file target, not a hard cap.
- Message Manager and Channel Mirror keep their own busy timeout overrides.
- No extra checkpoint worker, polling, automatic VACUUM, or cache growth.
- Long readers can delay checkpoints; inspect WAL size if it grows.

Health displays the actual mode, SQLite version, version eligibility,
requested mode, WAL and SHM bytes. It does not trigger a checkpoint.
The Telegram backup uses the existing online backup API and explicitly
finalizes the destination in DELETE mode as a standalone file. Never copy
only the live main DB or delete its -wal/-shm files manually.

## Rollback

Stop all processes using the DB, set WAL_ENABLED=false (full env name above)
and restart. SQLite handles committed data during the mode transition.
Do not downgrade SQLite while WAL is active. If switching fails, resolve
the lock/storage issue before restarting. No server changes are performed by
this PR itself.
