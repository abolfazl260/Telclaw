# Telclaw repository bootstrap and transfer-location cache

## Why this change exists

Previously every Telegram channel crawl ran `MessageRepository.initialize()`.
That method migrated the entire message schema, seeded all built-in
normalization aliases, backfilled missing `messages.content_hash` values and
rewrote **every existing** `transfer_locations` row, even if not one new
message was collected. `collection/crawler.py` separately repeated
`ensure_duplicate_schema` on every channel.

## Normal operation

- `main.py` calls `MessageRepository().initialize()` before starting
  services, once per process/database file.
- A standalone crawler retains its `repository.initialize()` call for
  backwards compatibility, but the first call initializes that database;
  subsequent calls for the same file identity are cheap no-ops.
- New SQLite files or replaced databases at the same path are detected from
  their file identity and initialized independently.
- Schema migrations, legacy content hash backfill, alias default seeding
  and `transfer_locations` table/index creation do not run on each channel.
- Every AI normalization operation **still reads the currently enabled rules**
  from `normalization_aliases`. Back Office changes take effect without
  restart. **The 8-hour AI processing cycle is unchanged.**
- `MessageRepository.save_category_record()` continues normalizing incoming
  records. For `transferlist`, it updates only the message's cache row and
  avoids rewriting the cache when all canonical route values are identical.
- Existing cargo backfill actions in Back Office continue to synchronize
  changed route cache rows.

No schema or delivery-state migration is required. Existing raw text,
published Telegram messages and Advertio delivery statuses are unaffected.

## Manually rebuild historical `transfer_locations`

Full historical cache reconstruction is **not automatic** on startup,
crawler cycles or normal AI processing. To repair a pre-existing, missing
or stale transfer location cache, schedule maintenance and **back up the
production SQLite database first**.

From the repository root with the production virtual environment and
`TELCLAW_DB_NAME`/configuration pointing to the intended database:

```bash
python scripts/rebuild_transfer_locations.py
```

The default mode is **read-only preview**, reporting `scanned`, `missing`,
`different` and `written=0`.

After reviewing the preview and confirming a backup, run:

```bash
python scripts/rebuild_transfer_locations.py --apply
```

Only missing or changed `transfer_locations` entries are created/updated.
Existing `transferlist`, `messages`, raw source text, AI states, Advertio
states and Telegram delivery records are not rewritten. The explicit repair
commits in batches (250 cache entries by default) to bound write-lock time.
Avoid running it concurrently with high-volume crawler or publisher traffic.

## Performance and regression safeguards

- Verify startup first-call vs repeated per-channel initialization with tests.
- Verify the same cache row is not updated for an unchanged route.
- Verify changed/new cargo routes still update `transfer_locations`.
- Verify user-managed normalization rules are still re-read after a change.
- Verify manual preview does not modify rows; applying is idempotent.
