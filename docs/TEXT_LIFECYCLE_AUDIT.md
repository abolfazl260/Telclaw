# Message text lifecycle and read-only integrity audit

> Scope: Telclaw **Stage** architecture, GitHub issue [#19](https://github.com/abolfazl260/Telclaw/issues/19). This document and the companion audit tool describe current behavior; they do **not** migrate or modify data.

## Canonical database contract

The SQLite table `messages` has three distinct **column names**. They remain present for backward compatibility.

| Column | Stored meaning | Created / updated by |
| --- | --- | --- |
| `raw_text` | Original Telegram message content as extracted by the crawler, preserved for future reprocessing and raw-based duplicate detection. | `collection/crawler.py` sets this on first insert. Normal cleaning does not overwrite it. |
| `cleaned_text` | Derived text from the processing cleaner. `None`/`NULL` before a fresh message has been processed. | `processing/stages.py::CleanTextStage` and the processing services. |
| `text` | **Legacy compatibility copy**, not a separate canonical text source. Initially mirrors the crawler's raw payload; after successful processing it mirrors `cleaned_text`. | Crawler and both processing services; this column is also independently editable in Backoffice. |

The current `processing/cleaner.py::clean_text` performs **whitespace normalization only**, via `" ".join(str(text).split())`; it **does not strip emojis**, phone numbers, URLs, words, or punctuation. Future cleaner changes must not alter stored `raw_text`.

### Expected states, not universal guarantees

1. On a **new crawl**: `raw_text == text == original extracted Telegram payload` and `cleaned_text IS NULL`.
2. On **successful processing**: `text == cleaned_text == clean_text(raw_text)` in the normal path; `raw_text` remains unchanged. Existing legacy rows without `raw_text` may use the old `text` fallback.
3. On **pending, processing, or failed** rows, no blanket equality should be assumed.
4. **Historical and manually edited** rows can diverge: `backoffice_data.py::MESSAGE_EDITABLE` currently allows independent editing of all three columns. A `text` edit does not necessarily update the `cleaned_text` consumed by AI.
5. SQL `NULL` (value absent) and `''` (present but empty) are different; whitespace-only strings are **neither** empty nor `NULL` under the audit's exact-string checks.

### Existing consumers and compatibility constraints

- **Collection/storage:** `collection/crawler.py`, `storage/database.py::insert_message`, `storage/message_repository.py`.
- **Raw-based duplicate detection:** `processing/processing_service.py::_original_text` and `storage/database.py::get_previous_messages_by_sender` use `raw_text` first and may fall back to legacy `text`.
- **Cleaning/persistence:** `processing/stages.py`, `processing/processing_service.py`, `services/processing_service.py` write the cleaned value to both `text` and `cleaned_text`.
- **AI:** `ai/classification_service.py::_source_text` and `ai/ai_service.py::_source_text` prioritize non-empty `cleaned_text`, then `text`, then `raw_text`.
- **Delivery:** `delivery/advertio_service.py` uses legacy `text` as an input to rental-term inference; `delivery/telegram_transfer_publisher.py` selects `m.text`; `routed_publisher.py` favors `cleaned_text`/`raw_text`.
- **Backoffice:** `backoffice_data.py` exposes independent edit/filter support; `backoffice_web.py` renders database-backed fields and preview text.
- **Tests:** `tests/test_backoffice_data.py`, `tests/test_ai_category_classification.py`, and others exercise existing text-field compatibility.

**Do not DROP, rename, or remove the `messages.text` column as part of this task.** Never perform a repository-wide rename of the identifier `text`: Telegram event payloads (`message["text"]`), formatting locals, and the legacy `TelegramProcessor/` CSV interface use unrelated keys and must be preserved. Follow-up changes are separately tracked in issues [#20–#26](https://github.com/abolfazl260/Telclaw/issues).

## Optional read-only audit

The stand-alone script `scripts/audit_message_text_columns.py` imports only the Python standard library. It **does not** import `config`, call `initialize_db`, run schema migrations, change statuses, or post advertisements.

Run explicitly against an **existing** staging SQLite database, preferably a backup or snapshot:

```bash
python scripts/audit_message_text_columns.py --database /absolute/path/to/staging-backup.sqlite3
```

The database path is required. Opening uses a `file:` SQLite URI with `mode=ro`, and the only database statement is a grouped `SELECT`. A nonexistent file or missing schema is an error; the script never creates a replacement database. JSON output contains aggregate counts only, with no message bodies or message IDs.

Each `by_processing_status` group (including SQL `NULL` status as JSON `null`) includes:

| Metric | Meaning |
| --- | --- |
| `total_rows` | Number of messages in that processing state. |
| `raw_text_null`, `cleaned_text_null`, `text_null` | Exact SQL `IS NULL` counts. |
| `raw_text_empty`, `cleaned_text_empty`, `text_empty` | Exact `= ''` counts; whitespace-only content is not counted. |
| `processed_text_cleaned_different` | Processed rows where both fields are non-`NULL` and their values differ. |
| `processed_text_cleaned_one_null` | Processed rows where exactly one of `text` and `cleaned_text` is `NULL`. |
| `unprocessed_text_raw_different` | All non-processed states (including `NULL` status): rows with both fields non-`NULL` where `text` differs from `raw_text`. |

The `summary` object contains sums across all groups. These are **diagnostic counts, not evidence of data corruption**. For example, an admin edit or historical upgrade may explain a mismatch. The audit cannot prove that `raw_text` was never manually changed, infer deleted historical content, or determine the cause of divergence. Do not automatically correct records based on counts.

### Safety, deployment and follow-up

- The script is **not** wired to startup, the scheduler, the Backoffice UI, or migrations.
- It does not update rows, edit publication history, invoke AI, or send Telegram/Advertio requests.
- On a live busy database, prefer a consistent SQLite staging backup/snapshot to avoid lock/WAL concerns; protect the snapshot as it contains original user messages even though aggregate output does not.
- Validate with `python -m pytest -q tests/test_message_text_audit.py` and the repository's normal `python -m pytest -q tests`.
- The subsequent independent issues address crawler compatibility (#20), processing mirrors (#21), AI fallbacks (#22), delivery (#23), Backoffice controls (#24), **explicitly opt-in** historical reconciliation (#25), and cross-layer regression testing (#26). None authorizes dropping columns or rewriting raw content.


## Backoffice manual text-editing safeguards (issue #24)

The authenticated `/data?table=messages` editor keeps all three columns visible
and filterable under their existing SQL names. It now explains their distinct
roles and marks **processed** rows where `text` and `cleaned_text` differ,
including a mismatch between `NULL` and a non-`NULL` value.

- Editing **`raw_text`** requires the admin to tick an explicit confirmation
  acknowledging that the original Telegram source will be changed. This
  confirmation is validated by the server, not just JavaScript. The guard
  changes neither administrator permissions nor other editable columns.
- Editing **`text`** updates only that legacy compatibility field. Because
  AI prefers `cleaned_text`, this edit does **not** guarantee a change in
  AI input. An explanatory warning is displayed.
- Editing **`cleaned_text`** updates only that field **by default**. An
  optional, unchecked checkbox enables **also update legacy `text`**.
  The editor sends both previously read values. The server rejects stale
  values and updates both columns in **one SQLite UPDATE/transaction**; it
  audits each column independently in `backoffice_data_edits`. If the
  accompanying legacy `text` changed since the editor opened, **neither**
  value is updated.
- Changing a message's text fields does **not** automatically rerun
  classification/extraction, correct existing AI results, retract an existing
  publication, or resend any advertisement. Workflows for these operations
  remain independent.
- All normal pagination, column filters, SQL `NULL` versus `''`, CSRF,
  session checks, optimistic concurrency, and audited edits are preserved.

This feature performs **no** historical text reconciliation and **no** schema
migration. See issue #25 for a separately authorized, dry-run-first approach
to legacy data corrections.


## Explicit, conservative historical reconciliation (issue #25)

The standalone script `scripts/reconcile_message_text.py` runs **only when an
operator invokes it**. It does not initialize a database, create new tables,
run during startup, enqueue AI, or send Telegram/Advertio messages. It never
removes `messages.text` and never modifies `messages.raw_text`.

### Step 1: Read-only dry-run

Run against a **consistent staging backup**, not the active production DB:

```bash
python scripts/reconcile_message_text.py --database /safe/staging-copy.sqlite3
```

The default uses SQLite URI `mode=ro` and only `SELECT` queries. It
displays **aggregates by processing_status**, separate `NULL` and exact `''`
counts, reason counts, and up to ten example message row IDs. It **never
prints message text, phone numbers, credentials, or raw payloads**.

Possible reasons include pending/unprocessed records, missing/blank/non-text
`raw_text`, missing/blank/changed legacy `text`, previously populated or
inconsistent `cleaned_text`, recorded manual text edits, and missing
Backoffice audit-history infrastructure. Such rows are **not eligible**.
If `backoffice_data_edits` is unavailable or structurally incompatible,
the script cannot verify editing provenance and **skips all processed rows**.

Only rows satisfying **every** condition may be corrected:
- `processing_status='processed'`;
- `raw_text` is a nonempty, non-whitespace text string;
- `cleaned_text IS NULL` (not the same as `''`);
- `text == processing.cleaner.clean_text(raw_text)`, exactly;
- `backoffice_data_edits` is available and contains **no recorded edit** to
  any of these three fields for the row.

These rules deliberately leave many historical disagreements unresolved.
An empty recorded edit history **cannot prove** no direct SQL edits ever
happened; treat candidates as conservative suggestions and inspect the source
system's maintenance history before authorizing changes.

### Step 2: Explicit operator-approved apply (NEVER run automatically)

Stop concurrent writers and arrange an approved maintenance window. On an
isolated **staging DB** only, after reviewing the dry-run count (for example
`eligible_rows: 2`):

```bash
umask 077
python scripts/reconcile_message_text.py \
  --database /safe/staging-copy.sqlite3 \
  --apply \
  --expect-candidates 2 \
  --max-updates 2 \
  --batch-size 25 \
  --backup /safe/NEW-before-reconcile.sqlite3 \
  --manifest /safe/NEW-reconciliation-log.jsonl
```

All three file paths must differ, and backup/manifest destinations **must not
exist** beforehand. The operator supplies both paths; the tool uses exclusive
creation with mode `0600`. It creates and verifies a complete SQLite backup
(using the SQLite backup API plus `PRAGMA quick_check`) **before any
modification**. It compares candidate count/IDs in the backup to the scanned
source; disagreement aborts without updating the source. No existing
backups/manifests are overwritten, renamed or deleted.

`--expect-candidates` must exactly match the dry-run's eligible count, and
`--max-updates` must cover all eligible rows (maximum 500). The script refuses
partial automatic selection, missing arguments, zero candidates, or malformed
schemas. `--batch-size` bounds in-memory scan/update groups (1–500).
Within one `BEGIN IMMEDIATE` transaction it **rechecks source, legacy,
cleaned, status and manual-edit history** for each candidate before a
compare-and-swap `UPDATE messages SET cleaned_text=?`. Concurrently changed
candidates are skipped. Errors roll back **all** attempted updates.

The separate private JSONL manifest records preparation, candidate IDs, SHA-256
digests of proposed cleaned values, and final applied/skipped IDs (or a
`rolled_back` event on failure). It stores **no message body**. Keep the
backup and manifest together in an access-controlled location. Do not rely on
a bare SHA-256 digest to reconstruct any original text.

### Manual rollback / recovery

This script has **no destructive automated restore**. The **complete,
verified SQLite backup** is the recovery source. If rollback is approved:

1. Stop Telclaw and all other database writers. Check whether unrelated
   legitimate changes occurred **after** the backup; an entire-database
   rollback would lose them, so do **not** overwrite a live file blindly.
2. Retain the current DB and WAL/SHM files as an additional protected snapshot.
   Use SQLite's backup API to restore the before-reconcile snapshot into a
   **new** private file (e.g. with `umask 077`), never directly over the
   running database:

   ```bash
   umask 077
   python -c 'import sqlite3; src=sqlite3.connect("file:/safe/NEW-before-reconcile.sqlite3?mode=ro",uri=True); dst=sqlite3.connect("/safe/NEW-restored.sqlite3"); src.backup(dst); dst.close(); src.close()'
   ```

3. Verify `PRAGMA quick_check`, expected row counts and publication history
   in the restored copy. Follow the project's operational recovery process to
   replace the offline DB **only after** reviewing concurrent changes.
4. If any later updates need preserving, reconcile individual corrected
   `cleaned_text` cells manually using the manifest's IDs and the backup's
   values, with explicit compare-and-swap guards; do not blindly restore the
   entire old DB.

Dry-run and apply tests use temporary SQLite fixtures, including backups,
concurrent changes, audit-history ambiguity and transaction rollback.
