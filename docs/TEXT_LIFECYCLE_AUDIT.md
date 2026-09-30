# Message text lifecycle and read-only integrity audit

> Scope: Telclaw **Stage** architecture, GitHub issue [#19](https://github.com/abolfazl260/Telclaw/issues/19). This document and the companion audit tool describe current behavior; they do **not** migrate or modify data.

## Canonical database contract

The SQLite table \`messages\` has three distinct **column names**. They remain present for backward compatibility.

| Column | Stored meaning | Created / updated by |
| --- | --- | --- |
| \`raw_text\` | Original Telegram message content as extracted by the crawler, preserved for future reprocessing and raw-based duplicate detection. | \`collection/crawler.py\` sets this on first insert. Normal cleaning does not overwrite it. |
| \`cleaned_text\` | Derived text from the processing cleaner. \`None\`/\`NULL\` before a fresh message has been processed. | \`processing/stages.py::CleanTextStage\` and the processing services. |
| \`text\` | **Legacy compatibility copy**, not a separate canonical text source. Initially mirrors the crawler's raw payload; after successful processing it mirrors \`cleaned_text\`. | Crawler and both processing services; this column is also independently editable in Backoffice. |

The current \`processing/cleaner.py::clean_text\` performs **whitespace normalization only**, via \`" ".join(str(text).split())\`; it **does not strip emojis**, phone numbers, URLs, words, or punctuation. Future cleaner changes must not alter stored \`raw_text\`.

### Expected states, not universal guarantees

1. On a **new crawl**: \`raw_text == text == original extracted Telegram payload\` and \`cleaned_text IS NULL\`.
2. On **successful processing**: \`text == cleaned_text == clean_text(raw_text)\` in the normal path; \`raw_text\` remains unchanged. Existing legacy rows without \`raw_text\` may use the old \`text\` fallback.
3. On **pending, processing, or failed** rows, no blanket equality should be assumed.
4. **Historical and manually edited** rows can diverge: \`backoffice_data.py::MESSAGE_EDITABLE\` currently allows independent editing of all three columns. A \`text\` edit does not necessarily update the \`cleaned_text\` consumed by AI.
5. SQL \`NULL\` (value absent) and \`''\` (present but empty) are different; whitespace-only strings are **neither** empty nor \`NULL\` under the audit's exact-string checks.

### Existing consumers and compatibility constraints

- **Collection/storage:** \`collection/crawler.py\`, \`storage/database.py::insert_message\`, \`storage/message_repository.py\`.
- **Raw-based duplicate detection:** \`processing/processing_service.py::_original_text\` and \`storage/database.py::get_previous_messages_by_sender\` use \`raw_text\` first and may fall back to legacy \`text\`.
- **Cleaning/persistence:** \`processing/stages.py\`, \`processing/processing_service.py\`, \`services/processing_service.py\` write the cleaned value to both \`text\` and \`cleaned_text\`.
- **AI:** \`ai/classification_service.py::_source_text\` and \`ai/ai_service.py::_source_text\` prioritize non-empty \`cleaned_text\`, then \`text\`, then \`raw_text\`.
- **Delivery:** \`delivery/advertio_service.py\` uses legacy \`text\` as an input to rental-term inference; \`delivery/telegram_transfer_publisher.py\` selects \`m.text\`; \`routed_publisher.py\` favors \`cleaned_text\`/\`raw_text\`.
- **Backoffice:** \`backoffice_data.py\` exposes independent edit/filter support; \`backoffice_web.py\` renders database-backed fields and preview text.
- **Tests:** \`tests/test_backoffice_data.py\`, \`tests/test_ai_category_classification.py\`, and others exercise existing text-field compatibility.

**Do not DROP, rename, or remove the \`messages.text\` column as part of this task.** Never perform a repository-wide rename of the identifier \`text\`: Telegram event payloads (\`message["text"]\`), formatting locals, and the legacy \`TelegramProcessor/\` CSV interface use unrelated keys and must be preserved. Follow-up changes are separately tracked in issues [#20–#26](https://github.com/abolfazl260/Telclaw/issues).

## Optional read-only audit

The stand-alone script \`scripts/audit_message_text_columns.py\` imports only the Python standard library. It **does not** import \`config\`, call \`initialize_db\`, run schema migrations, change statuses, or post advertisements.

Run explicitly against an **existing** staging SQLite database, preferably a backup or snapshot:

\`\`\`bash
python scripts/audit_message_text_columns.py --database /absolute/path/to/staging-backup.sqlite3
\`\`\`

The database path is required. Opening uses a \`file:\` SQLite URI with \`mode=ro\`, and the only database statement is a grouped \`SELECT\`. A nonexistent file or missing schema is an error; the script never creates a replacement database. JSON output contains aggregate counts only, with no message bodies or message IDs.

Each \`by_processing_status\` group (including SQL \`NULL\` status as JSON \`null\`) includes:

| Metric | Meaning |
| --- | --- |
| \`total_rows\` | Number of messages in that processing state. |
| \`raw_text_null\`, \`cleaned_text_null\`, \`text_null\` | Exact SQL \`IS NULL\` counts. |
| \`raw_text_empty\`, \`cleaned_text_empty\`, \`text_empty\` | Exact \`= ''\` counts; whitespace-only content is not counted. |
| \`processed_text_cleaned_different\` | Processed rows where both fields are non-\`NULL\` and their values differ. |
| \`processed_text_cleaned_one_null\` | Processed rows where exactly one of \`text\` and \`cleaned_text\` is \`NULL\`. |
| \`unprocessed_text_raw_different\` | All non-processed states (including \`NULL\` status): rows with both fields non-\`NULL\` where \`text\` differs from \`raw_text\`. |

The \`summary\` object contains sums across all groups. These are **diagnostic counts, not evidence of data corruption**. For example, an admin edit or historical upgrade may explain a mismatch. The audit cannot prove that \`raw_text\` was never manually changed, infer deleted historical content, or determine the cause of divergence. Do not automatically correct records based on counts.

### Safety, deployment and follow-up

- The script is **not** wired to startup, the scheduler, the Backoffice UI, or migrations.
- It does not update rows, edit publication history, invoke AI, or send Telegram/Advertio requests.
- On a live busy database, prefer a consistent SQLite staging backup/snapshot to avoid lock/WAL concerns; protect the snapshot as it contains original user messages even though aggregate output does not.
- Validate with \`python -m pytest -q tests/test_message_text_audit.py\` and the repository's normal \`python -m pytest -q tests\`.
- The subsequent independent issues address crawler compatibility (#20), processing mirrors (#21), AI fallbacks (#22), delivery (#23), Backoffice controls (#24), **explicitly opt-in** historical reconciliation (#25), and cross-layer regression testing (#26). None authorizes dropping columns or rewriting raw content.
