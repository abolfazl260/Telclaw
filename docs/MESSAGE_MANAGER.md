# Message Manager

Message Manager publishes only drafts explicitly created/imported by a Back Office administrator. It uses its own `mm_*` tables and independent worker, never reads crawler messages or AI queues, and does not create publishing rules or change existing ad deliveries. It is enabled by default: the tab and worker are available automatically, with no feature activation step.

## Enable and operate

1. Back up the SQLite database and install `requirements.txt`.
2. Keep the existing bot token configured and use a private `TELCLAW_MESSAGE_MANAGER_MEDIA_PATH` outside any web server's static root. Give the service account read/write access. Only authenticated media routes serve these files; never expose the directory through a reverse proxy. No `TELCLAW_MESSAGE_MANAGER_ENABLED` setting is required; an explicit `false` remains an operator opt-out for rollback.
3. Restart `python main.py`. Additive schema creation happens at startup. The Message Manager worker starts even when Back Office is disabled or fails to bind. Serving only `create_app()` without the normal runtime does **not** start a worker.
4. Open the existing administrator link from Telegram and select **Message Manager**. Select **Check and enable** for existing destinations, or register a new channel. The bot must be a channel administrator with posting permission. New channels are registered with automatic ad publishing disabled. Message Manager eligibility is independent of the ad-publishing `enabled` flag.
5. Create and save a draft. Use **Preview**, choose destinations, and explicitly confirm **Queue Publish Now** or **Activate schedule**. These actions enqueue work; HTTP handlers do not send publications. Publishing uses the saved draft, so save edits first.

The editor accepts plain text or a safe subset of Telegram HTML: `b/strong`, `i/em`, `u/ins`, `s/strike/del`, `a href`, `code`, `pre`, `blockquote` (optionally expandable), and `tg-spoiler`. No raw scripts, arbitrary HTML attributes, external preview images, or embedded HTML are allowed. Preview uses the same sanitized content as the transport; Telegram clients may render typography/spoilers differently. Limits are conservatively measured in UTF-16 units: 4096 for text, 1024 for captions. Inline keyboards are JSON arrays of rows, for example `[[{"text":"Open","url":"https://t.me/example"}]]`. HTTP(S) links and supported `tg://user`, `resolve`, `join`, and `privatepost` deep links are allowed.

Each publication supports one JPEG/PNG photo, MP4 video, or document (PDF, UTF-8 TXT, ZIP, DOCX, XLSX). Upload generates a private filename and an asset key; save that key on the draft or use it in the XLSX template. Files are streamed in bounded chunks, checked against extension/MIME and file signatures, hashed, and stored with restrictive permissions. Photo dimensions follow Telegram's constraints. Cached Telegram file IDs are scoped to the bot token fingerprint. Albums are deferred; the relationship table supports future multiple media positions.

## Scheduling and delivery

All form and spreadsheet schedule dates are **Gregorian dates in Asia/Tehran**. `zoneinfo` converts local wall time to UTC; executable timestamps are persisted in UTC. The list and delivery history display Tehran time. Creation-date filters are explicitly labeled UTC.

Supported recurrence values:

| Value | Behavior |
| --- | --- |
| `once` | One publication at the local date/time |
| `daily` | Every local day |
| `weekly` | Selected weekdays every week |
| `days` | Every N days, anchored to the start date |
| `weeks` | Selected weekdays every N weeks, anchored to the Monday of the start week |

Weekdays are `0=Monday` through `6=Sunday`, comma separated. Blank weekdays select the start date's weekday. The end date is inclusive; an end count bounds the total recurrence slots, including missed slots. Neither limit means indefinite recurrence. Dates earlier than the start are never generated. A schedule stores an immutable content/version and destination snapshot. Editing a draft affects future **new schedules**, not an already activated schedule. Use the optional replacement schedule ID to atomically cancel an active/paused schedule and create its replacement.

Schedules, occurrences, per-channel deliveries and attempt history are separate. `(schedule_id, ordinal)` and `(occurrence_id, target_id)` are unique. SQLite `BEGIN IMMEDIATE` transactions serialize materialization and claims across threads/processes. Claims and attempts commit before external sends, with a 300-second lease. Worker restart recovers only expired leases as **Uncertain**, so a second worker never immediately steals a live send. All runtime processes must share the same SQLite file and media storage; avoid SQLite on filesystems without reliable locking.

- A one-time publication may run up to 24 hours late by default; older occurrences become **Missed**.
- Recurring publications may run up to five minutes late by default. Older backlog is recorded as missed once, then fast-forwarded to the next future slot. It never floods old recurrence slots.
- Pause blocks pending work. On resume, the same missed-event policy applies. Cancel/archive cancels unclaimed work. A send already in progress may finish; cancellation cannot revoke a request Telegram already accepted.
- Each destination retains its own successful outcome when other channels fail. The detail view shows the latest 200 deliveries, 500 attempts, final errors, UTC-persisted retry times displayed in Tehran, and Telegram IDs/links when returned.
- Structured Bot API 429/5xx rejections and connection-establishment failures are definitively retryable. At most **three total attempts** occur by default, using 30/60-second exponential delays; a longer Telegram `retry_after` takes precedence. Permission checks that fail transiently are safe to retry because they do not publish.
- Bot-wide 429 cooldown is shared through the existing `publishing_targets.rate_limited_until` registry. Message Manager also reserves persistent global/per-target send slots across its workers. Existing ad publisher behavior remains unchanged; its sends are not serialized with Message Manager outside the existing shared cooldown mechanism.
- 403, invalid targets/content and revoked eligibility fail with actionable errors. Permission is checked on activation and again before each send. Changed registry chat IDs fail rather than redirecting an existing scheduled publication.
- Timeouts, unreadable/ambiguous send responses and expired sending leases become **Uncertain**. They never automatically resend. Verify the channel manually before checking the duplicate-risk acknowledgement and queuing a manual resend. Manual resends preserve attempt history and add one explicit attempt; once the configured automatic attempt budget is spent, further failures require another manual decision.

Each mutation is recorded in `mm_audit`, visible on the page, and in the existing system activity timeline when initialized. Imported workbook hashes, actors and confirmation states are retained.

## XLSX workflow

Download the template from the authenticated page. The workbook contains exactly `Messages` and `Buttons`.

- Required `Messages` headers: `message_key`, `channels`, `message`, `date`, `time`, `recurrence`. Optional columns: `title`, `asset_key`, `weekdays`, `interval`, `end_date`, `end_count`, `parse_mode`.
- Required `Buttons` headers: `message_key`, `button_text`, `button_url`. Optional `row` and `order` set placement; omitted order appends within the row.
- Destinations are semicolon-separated stored `@channels` or target IDs. Enable Message Manager eligibility first. File URLs are never fetched; use an uploaded `asset_key`.
- Choose an import namespace. Keys must be unique in the workbook and namespace. Existing imported keys are rejected; scheduled content is never silently replaced.
- Upload **validates only**, producing persisted row-level errors and a complete content/destination/schedule preview. Correct all errors before proceeding. **Create imported drafts** requires confirmation and creates no schedules.
- **Activate imported schedules** is a second explicit confirmation. Activation is atomic for the batch, rechecks permissions/eligibility, rejects changed/archived drafts, and rejects dates that have passed. If a date has passed, schedule the imported draft manually instead. Repeating confirmation is idempotent; repeating activation is rejected. Recent imports remain accessible on the page after reconnecting.
- Formula cells, macro workbooks, unknown sheets/headers, unsafe links, excess rows and excessive ZIP expansion are rejected. Status export includes the latest 10,000 deliveries and writes escaped text cells to prevent spreadsheet-formula injection.

This import is independent of the existing external-ad CSV importer.

## Configuration

| Environment variable | Default / bound |
| --- | --- |
| `TELCLAW_MESSAGE_MANAGER_ENABLED` | `true`; no setting required, explicit `false` disables the module |
| `TELCLAW_MESSAGE_MANAGER_POLL_SECONDS` | `2`, minimum `0.5` |
| `TELCLAW_MESSAGE_MANAGER_BATCH_SIZE` | `25`, capped at `100` |
| `TELCLAW_MESSAGE_MANAGER_MAX_ATTEMPTS` | `3`, capped at `10` |
| `TELCLAW_MESSAGE_MANAGER_MISSED_WINDOW_SECONDS` | `86400` |
| `TELCLAW_MESSAGE_MANAGER_RECURRING_GRACE_SECONDS` | `300` |
| `TELCLAW_MESSAGE_MANAGER_SEND_SPACING_SECONDS` | `1.1`, minimum `1` |
| `TELCLAW_MESSAGE_MANAGER_MEDIA_PATH` | `private/message-manager` |
| `TELCLAW_MESSAGE_MANAGER_MEDIA_MAX_BYTES` | `20971520`; hard cap 50 MiB, photos 10 MiB |
| `TELCLAW_MESSAGE_MANAGER_MEDIA_RETENTION_DAYS` | `30` |
| `TELCLAW_MESSAGE_MANAGER_XLSX_MAX_BYTES` | `1048576`, capped at 1500 KiB |
| `TELCLAW_MESSAGE_MANAGER_XLSX_MAX_ROWS` | `1000`, capped at `1000` |

Media uploads use a dedicated authenticated, header-CSRF-protected streaming endpoint, without increasing the application's existing 2 MiB limit for ordinary forms. XLSX multipart requests remain within that limit. Configure any reverse proxy's upload limit specifically for `/message-manager/media`, and keep request/connection timeouts appropriate for your maximum file size.

Retention removes only old unreferenced assets in bounded periodic batches. Assets referenced by drafts, schedule snapshots or unconfirmed import previews remain available; referenced publication media and all delivery/audit/import records are retained. Back up the database **and** private media directory together. A missing file fails that delivery; it does not trigger arbitrary remote downloading.

## Verification and recovery / rollback

Run `TELEGRAM_API_ID=12345 TELEGRAM_API_HASH=test python -m pytest -q tests`. New tests cover recurrence/timezone/end conditions, simultaneous scheduling/claims, partial success, 429/backoff/attempt caps, restart recovery, manual duplicate-risk acknowledgement, media security/retention, XLSX validation/idempotency/activation/export, authentication/CSRF, large streaming uploads and route integration. Telegram is mocked; no real channel is published to by tests.

For initial rollout, enable the module on a staging database and test bot/channel, create a short one-time schedule and verify message ID/link and one attached file in Telegram. Real bot permissions, file acceptance, proxy behavior and Telegram client rendering require this deployment smoke test.

To stop new dispatch, set `TELCLAW_MESSAGE_MANAGER_ENABLED=false` and restart all runtime processes. The tab/routes/worker disappear; existing crawler/AI/ad publishing continues. No tables or records are dropped. Stop processes before restoring a matched database/media backup. Re-enabling applies missed-event policy and recovers expired sending leases as uncertain; verify those manually. Do not reset uncertain deliveries to waiting with SQL. Avoid reverting to a database snapshot while leaving newer Telegram sends unverified, since Telegram cannot roll them back or deduplicate them.

## Organized workspace

Message Manager has four internal tabs: **Messages**, **Create message**, **Channels**, and **Excel and reports**. The list starts with an empty-state action when no messages exist and shows pagination only when needed. Opening a saved message selects the editor; import previews and confirmations open in the Excel tab. Channel actions return to Channels.

The responsive editor places the Telegram preview alongside the form on desktop and below it on mobile. Write the text, attach a file, and add button text/URLs with row numbers. Raw keyboard JSON is available under Advanced keyboard JSON. Bold, italic and link tools insert supported Telegram HTML around the selected text. The preview is validated by the server and never queues a publication. Uploaded attachments are identified by filename with a protected media preview, and can be removed from the draft.

Save the draft before choosing destinations or activating a schedule. Publication uses saved content; the editor warns and blocks publication when local changes have not been saved. Without JavaScript, the server-rendered tabs, advanced JSON field, manual Preview and Save actions remain available.
