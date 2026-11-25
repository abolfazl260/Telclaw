# Automatic SQLite journal policy

No environment variable or Back Office switch is required. At main service
startup, before schema migration and worker startup, Telclaw checks the SQLite
runtime and the actual database mount. WAL activates automatically when both
checks pass. Otherwise it retains a safe rollback journal mode.

- Upstream WAL-reset fix: >=3.51.3, 3.50.x >=3.50.7 or 3.44.x >=3.44.6.
  See https://sqlite.org/wal.html#the_wal_reset_bug . Unknown vendor backports
  are not assumed safe.
- Linux /proc/self/mountinfo: deepest resolved mount must be ext2/ext3/ext4,
  XFS, Btrfs, F2FS or ZFS. Network, FUSE, overlay and unknown mounts (including
  platforms without mountinfo) are deliberately excluded. Mount type alone
  cannot guarantee that a shared block device is not accessed by other hosts.
  All database users must run compatible SQLite builds on a single host.
- If activation fails but the actual journal is verified as DELETE, TRUNCATE
  or PERSIST, continue in rollback mode and log a warning.
- Existing incompatible WAL must be converted to DELETE successfully before
  normal startup. If conversion cannot be verified, stop startup for safety.
  Never delete WAL/SHM sidecars manually.
- No mode renegotiation on routine connections. Standalone entrypoints do not
  automatically activate WAL; shared connection helper refuses an unsafe
  SQLite runtime for an existing WAL database.

Before first deployment, take a verified SQLite backup and stop all other
processes accessing the database during service restart. Startup logs and
Health > Database show the actual journal mode, version, mount type and
eligibility status. An eligible but inactive WAL indicates a transition was
not completed. This is diagnostic, not a configuration UI.

## Settings and backup

- synchronous=FULL, foreign_keys=ON, busy_timeout=5000 ms.
- WAL auto-checkpoint 1000 pages; retained journal target 16 MiB (not a hard
  cap). Existing service-specific timeout overrides are unchanged.
- No extra worker, automatic VACUUM, schema migration or cache growth.
- Existing Telegram /database uses SQLite online backup and exports a
  standalone DELETE-mode snapshot, including committed WAL transactions.
- Never copy only the live main file. WAL can grow with long-lived readers.

There is no persistent manual override. For emergency rollback, stop all
database users, use a patched SQLite runtime to issue PRAGMA journal_mode=DELETE,
and resolve the environmental problem before restarting Telclaw (which
re-evaluates eligibility). Do not downgrade SQLite while WAL is active.
