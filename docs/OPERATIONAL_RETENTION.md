# Operational retention

Defaults requested for Telclaw:

| Data | Retention |
| --- | --- |
| DEBUG logs | 84 hours (3.5 days) |
| INFO logs | 15 days |
| WARNING / ERROR / CRITICAL logs | 45 days |
| Historical heartbeat activity | 84 hours |
| Unreferenced Message Manager uploads | 15 days (existing env override applies) |
| Successfully delivered housing media | Existing immediate best-effort cleanup |
| Raw messages and delivery identity/state | Preserved |

The application rotates Python file logs into 12-hour UTC intervals and runs a
bounded maintenance pass 60 seconds after startup, then every 12 hours.
Rotated intervals are removed after their entire interval expires, so actual
removal can lag by up to two intervals. The original ERROR_LOG_FILE path is
preserved for warnings/errors; .info and .debug are separate files. The existing
root ERROR threshold is preserved to avoid increasing logging volume; this
policy applies to INFO/DEBUG records when enabled. Console/stdout/journald logs
are external to Python logging and need their own host retention configuration.

The system_activity table is pruned in batches of 500, at most ten batches per
severity per pass. The most recent non-log INFO/DEBUG event of each kind is
preserved for health/stage reporting. Current heartbeat state stored separately
is not deleted. No VACUUM is run automatically; SQLite can reuse freed pages.

The read-only SQLite view archived_expired_cargo provides a logical archive for
cargo with an explicit departure date before the current UTC date. Raw messages,
category rows, deduplication keys, publishing history and retries remain in their
original tables. No physical migration or deletion of listings is performed,
and the archive does not change existing publishing eligibility or reduce
database size. Housing and job listings lack a reliable expiry field and are
not archived by guesswork. Inspect using SELECT * FROM archived_expired_cargo.

Existing media references and scheduled Message Manager attachments are kept.
An explicit TELCLAW_MESSAGE_MANAGER_MEDIA_RETENTION_DAYS=30 in a deployed .env
must be changed to 15 if the new default is desired. No arbitrary filesystem
sweep is performed, to avoid deleting in-flight or referenced media.
