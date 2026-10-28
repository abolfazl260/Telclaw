# Telclaw publishing back office

The back office is disabled by default. It runs inside the existing Python process
and stores targets, rules, login sessions, and per-destination delivery records in
the existing SQLite database. Only Telegram admins `1485409432`, `266809220`, and
`7469291969` can request a login link in a private chat with the monitoring bot.

## Start

1. Give the bot permission to post in every destination channel or group.
2. Put these settings in the server's `.env` file:

   ```dotenv
   TELCLAW_TELEGRAM_MONITOR_ENABLED=true
   TELCLAW_TELEGRAM_BOT_TOKEN=your_bot_token
   TELCLAW_BACKOFFICE_ENABLED=true
   TELCLAW_BACKOFFICE_PUBLIC_URL=https://your-private-domain.example
   TELCLAW_BACKOFFICE_PUBLIC_PORT=0
   TELCLAW_BACKOFFICE_HOST=127.0.0.1
   TELCLAW_BACKOFFICE_PORT=8787
   ```

3. Configure a reverse proxy with a valid HTTPS certificate to forward the public
   URL to `http://127.0.0.1:8787`. Do not expose port 8787 directly to the internet.
   The browser session cookie is marked `Secure`, so the public URL must use HTTPS.
   For direct access by server IP and port without a reverse proxy, set:

   ```dotenv
   TELCLAW_BACKOFFICE_PUBLIC_URL=https://SERVER_PUBLIC_IP:8787
   TELCLAW_BACKOFFICE_PUBLIC_PORT=0
   TELCLAW_BACKOFFICE_HOST=0.0.0.0
   TELCLAW_BACKOFFICE_PORT=8787
   TELCLAW_BACKOFFICE_TLS_CERT=/path/to/ip-certificate.pem
   TELCLAW_BACKOFFICE_TLS_KEY=/path/to/private-key.pem
   ```

   The certificate must be valid for that IP address. Allow incoming TCP 8787
   through the server firewall. Keep the TLS private key readable only by the
   service account. Restart Telclaw and request a fresh link from the bot.
   If you explicitly want direct HTTP without TLS, use:

   ```dotenv
   TELCLAW_BACKOFFICE_PUBLIC_URL=http://SERVER_PUBLIC_IP:8787
   TELCLAW_BACKOFFICE_PUBLIC_PORT=0
   TELCLAW_BACKOFFICE_HOST=0.0.0.0
   TELCLAW_BACKOFFICE_PORT=8787
   TELCLAW_BACKOFFICE_TLS_CERT=
   TELCLAW_BACKOFFICE_TLS_KEY=
   ```

   Open TCP port 8787 in the firewall. Direct HTTP exposes the one-time login
   link and browser session to anyone who can observe the network connection.
   To include a port in the Telegram link, set `TELCLAW_BACKOFFICE_PUBLIC_PORT=8443`
   and configure the HTTPS reverse proxy to listen on 8443. A public IP can replace
   the domain in `TELCLAW_BACKOFFICE_PUBLIC_URL` only if HTTPS has a certificate valid
   for that IP. The public port and local 8787 port serve different purposes.
4. Restart Telclaw and send `/backoffice` to the bot in a private chat from an
   authorized Telegram account. The link expires after one hour and can be
   redeemed only once. It establishes a 12-hour browser session. Use **Log out**
   when finished.

## AI Provider Status in Settings

The **Settings → AI Provider Status** panel shows the configured priority,
credential-set count and non-secret model names for Groq and Cloudflare.
It also shows which provider is currently selected by an initialized
Classification or Extraction manager, if those managers exist in this
Back Office process. "Eligible" means only that no observed cooldown is
active; it is **not** an external API health check or proof of a valid key.

Groq's **estimated available tokens** and **safe TPM** come directly from
the process-local shared token budget used by Classification and Extraction
and by all Groq keys. This is a conservative approximation, not the exact
Groq console quota and not an independent token balance for each key.
Until any client creates a local budget, the available value is shown as
**Unknown**, not as the full configured limit. A configured safe TPM is still
shown as context. Cloudflare's token balance is **Unknown** because the
current Workers AI integration does not expose a consistent remaining-token
counter. Per-account cooldown is shown when observed.

The panel refreshes from an authenticated **GET
`/settings/ai-providers/status`** endpoint every 15 seconds while the tab
is visible, with a manual refresh button. It **never sends provider API calls**
to obtain status or uses provider credentials in the response. All data is
process-local: separate Telclaw workers, hosts or requests outside Telclaw
cannot be monitored by this panel. Settings changes requiring service client
reconstruction still require a restart. Existing database and publication
behavior are unchanged.

## SQLite concurrency in the Telegram publisher

The Telegram publisher's stale-delivery recovery probes for old `sending`
rows using read-only queries before attempting to update any delivery state.
On normal healthy cycles (no stale send/resend), it does not take an unnecessary
SQLite write lock. Sends that truly exceed the five-minute recovery window
remain `uncertain`, never silently retried, because Telegram has no
delivery idempotency key.

If a concurrent crawler, AI or administrative backfill transaction holds the
SQLite write lock during **queue preflight**, the current publisher cycle
defers its entire queue without attempting any Telegram sends. This is logged
as a warning rather than a cycle failure. The next normal scheduled cycle
checks again; this is not an automatic retransmission of any uncertain send.
Real database errors other than SQLite busy/locked still propagate for alerting.

## Fast Normalization page

The **Normalization** page offers one primary **Cargo Location
Normalization** operation. Use **Preview Changes** to calculate affected
origin/destination country, city and missing province fields (read-only and
only on demand). Then use **Apply Normalization** to update those six cargo
location fields in a single SQLite transaction. Countries are standardized
first, then cities, then *missing* provinces. Explicit province values,
other ad fields, source messages and delivery statuses remain unchanged.

The older **Re-normalize existing data** action is preserved inside a
collapsed **Advanced Normalization** section for any category or all fields.
Legacy route-only and province-only endpoints remain available for backwards
compatibility but are no longer separate primary page actions.

Below these actions, **Add normalization alias** is available immediately.
Previously saved rules are hidden by default: the initial page counts rules
with a single SQL aggregate without rendering rule forms or scanning category
rows per rule. Choose **Load first 20 rules** to inspect/edit a page of up to
20 rules, then **Load next 20** or **Previous 20** to browse. The **Cargo
city/country rules** filter applies before pagination. **Check affected rows**
on an individual rule calculates its potentially changed-row count only on
demand. No existing aliases, save/delete actions, or country scopes were
removed; the change is presentation/query loading only.

## Match the Telegram /transferlive active-cargo count

The **Active Cargo** page now displays two counts and has a **Report definition**
selector. The Advertio candidate view includes future-dated transfers and recent
undated ads (last configured backfill window) without trip deduplication. The
**Match Telegram /transferlive** view uses the precise ID cohort underlying
`/transferlive`: only today/future dated trips with valid origin/destination
country codes, deduplicated by sender, route and departure date. Its total
therefore matches the Telegram bot's active-trip count. The Ready-for-API
counter is a narrower intersection with the API's supported countries, cities,
processing state and delivery retry rules. Neither metric confirms original
Telegram messages still exist.

Telegram's command menu is registered from one authoritative command list for
both default and all-private-chat scopes. `/transferlive`,
`/transfercountry` and `/aboutus` remain available simultaneously after the
Telclaw Telegram monitor restarts.

## Download only filtered database rows

In **Database**, open one table (`messages`, `transferlist`, `housinglist`,
or `joblist`), choose column filters and click **Apply filters**. The
**Download filtered CSV (N rows)** link then downloads **all N matching rows**
from that selected table across every pagination page. It does not export
other tables, unfiltered rows, or the full SQLite database. The export endpoint
refuses requests without at least one effective filter (a blank text filter
is not effective). All operators, typed values and NULL rules use the exact
same validated query builder as the on-screen list.

The download is streaming UTF-8 CSV with an Excel-friendly BOM. Multilingual
text is preserved; strings that spreadsheet programs could evaluate as
formulas receive a safe leading apostrophe. Downloads use the existing
authenticated Back Office session and are never exposed via a public link.

## Operations tab

The **Operations** tab mirrors the operational controls from the terminal UI while
calling the same application services and sharing the same in-process crawler/runtime
state. Long-running queue commands are started as background jobs so the HTTP request
returns immediately; while a manual job is active the page refreshes automatically and
shows the current queue counts plus the last result/error.

The page is grouped into numbered sections with shortcut links: processing pipeline,
Advertio delivery, diagnostics, unsent legacy transfer ads, scheduled crawler, Telegram
accounts, channel inventory, settings, and operation history. Use the sections in order
when processing new data. Manual queue buttons start a one-off run; they do not change
the scheduled crawler configuration.

The **Active Cargo** tab (/cargo) provides a searchable, 25-per-page report of
potentially active transfer ads: upcoming dated trips or recent undated posts
within the configured backfill window. Search by Telegram ID, channel, route
city or title, filter by upcoming/undated trip and Advertio delivery status,
and view the original Telegram post, description and API error.

The **Ready for API** metric applies the route-completeness, supported-country,
delivery-state and retry-time gates. This is not proof the original Telegram
message still exists; the platform checks canonical cities, content and
duplicates on ingest. Select up to 25 eligible records on the displayed page
and click **Send selected active cargo to Advertio**, or click **Send next 25
ready ads** for automatic batch ordering. Both operations reuse the existing
cargo-only idempotent queue, configured daily budget, and shared pipeline
lock. Sent/rejected records cannot be resubmitted through this report.
The existing Telegram and Housing publishers are unaffected.

The **Advertio Cargo — Website API** card is separate from the Telegram @advertio_cargo
publisher and the housing delivery queue. Its auto-publish setting applies to newly
submitted cargo listings only after API validation and duplicate checks. **Sent to API**
means the API accepted the request; it does not prove that a listing is publicly visible.
Turning auto-publish on does not activate existing PendingReview listings.

Available controls include:

- start/stop the information-processing queue;
- start/stop AI category classification with a chosen batch size;
- requeue failed classifications;
- start/stop AI extraction using an authorized Telegram session for media recovery;
- send eligible waiting/retry housing listings to Advertio;
- run the Groq connectivity diagnostic;
- schedule/stop the same recurring multi-category crawler used by the terminal;
- view transfer-ad queue status and jump to rule-based Publishing;
- list crawler channel groups;
- select/switch/disconnect Telegram sessions;
- add a Telegram account through the browser verification-code and optional 2FA flow;
- open the full Back Office Settings and System Health pages.

Manual processing/classification/AI/Advertio runs share the scheduler's pipeline lock,
so an operator-started queue job cannot overlap the scheduled post-crawl pipeline.
Processing, classification and AI controls expose a stop request that takes effect at
the next safe worker boundary. Back Office operational starts/completions/errors are
also written to the existing `system_activity` timeline.

With Back Office enabled, the old terminal **Transfer Ads → Send in channel** action
remains intentionally disabled; destination delivery is handled by the Back Office
Publishing rules instead.

## System Health tab

The **System Health** tab is a read-only operational dashboard. It shows SQLite
integrity and size, tracked table row counts, current processing/classification/AI/
Advertio queues and failures, recent crawl totals by day, and every configured or
observed crawl channel with its first/last stored message and failure count.

Crawler, processing, classification, AI and Advertio reports emitted by the Telegram
monitor are now persisted in the `system_activity` table before they are broadcast.
System errors are persisted there as well, so the health tab keeps a recent activity
timeline across browser refreshes and process restarts. The page also reports Telegram
monitor runtime state, active report subscribers, publishing delivery/resend totals,
and recent audited database edits. Configured channels that have never produced a
stored message are shown as **not crawled** instead of silently disappearing.

The dashboard, Telegram `/health`/`/status` commands and the terminal's
**System Health** menu option 12 use `services/health_metrics.py` for shared
queue eligibility and stage health. **Pipeline backlog** and **Failed items**
count distinct messages, not the sum of stage counters: a retryable failed
classification can appear in both figures, but a message is counted once within
each figure. Advertio queue eligibility requires a processed housing listing
and a real `housinglist` record; jobs/transfers are not Advertio backlog.
Rejected housing deliveries count as failures. Category classification pending
includes retryable failures up to the configured retry budget.

**Last crawl** comes from the newest persisted `system_activity` crawl event
(including zero-message crawls); other last-activity values come from stage
completion timestamps and persisted stage events, never the source message date.
Old installations without crawl events display **Not recorded** rather than a
fabricated midnight/03:30 time. Times are UTC in Back Office and Tehran-local
in Telegram/terminal. A 24-hour freshness window is used for reporting only.
Stage statuses can be HEALTHY, WARNING, CRITICAL (stale with actionable
queue work), or DISABLED. Historical failed message states remain visible in
the failed counters but do not by themselves make a stage CRITICAL; an idle
stage with stored failures or stale activity is WARNING. Pending/eligible
counters show current runnable work, not records that have already failed.
The Advertio eligible count uses the same processing-status and housing-row
gates as the actual terminal delivery queue. HEALTHY measures observed activity and queue state; it is
not an external AI/Advertio API probe. Database health uses SQLite
`PRAGMA quick_check`. Without a `messages` table, health displays an empty
warning state instead of failing.

The dashboard itself remains read-only. Use the **Operations** tab to start, stop or
retry pipeline work, and use the page's **Refresh** link to request a fresh snapshot.

## Database tab

The **Database** tab shows `messages`, `transferlist`, `housinglist`, and `joblist`
as paginated tables (25 rows per page, newest IDs first). Scroll horizontally to
inspect every column. Click a blue cell to edit its complete value or set it to
`NULL`, then save. Updates are checked against the value loaded when the cell was
opened; if the crawler changed it meanwhile, reload the cell before saving again.
Every successful edit is recorded in `backoffice_data_edits` with the admin ID.
IDs, foreign keys, pipeline status fields, and authentication tables cannot be
edited here. Changes to ad fields are immediately available to publishing rules;
already sent ads are not automatically resent.

## Rules

Add each **publishing channel / group** with its Telegram username or negative chat ID
and an optional description. A channel/group is only the place where matched ads are
published; it is not a transport "destination" and does not imply any business topic.
The bot checks membership and posting permission when you save it. Use **Check connection**
to refresh that state.

Rules are topic-agnostic. Pick a structured category, then build zero or more conditions
from the columns that actually exist in that category's SQLite table. The back office
discovers those fields with SQLite schema metadata instead of maintaining a transport
field list in the UI. This means housing, jobs, transport, and future structured topics
all use the same rule engine. A newly added column is automatically available to rules
without adding a custom country/city/origin/destination control.

Each condition contains a database field, an operator, and (when needed) a value. Multiple
conditions can be connected with **AND** or **OR** and are evaluated from left to right.
Supported operators are equality/inequality, contains/not-contains, numeric comparisons,
and empty/not-empty. Stored values from the selected field are loaded as suggestions,
but admins can type another value when appropriate. The optional source-channel filter,
priority, enabled state, continuation behavior, and manual/automatic publishing mode are
shared controls that apply to every topic.

Older rules created with the previous country/origin/destination/price columns remain
compatible. When such a rule is opened, those legacy settings are translated into the
generic condition builder so saving the rule moves it to the topic-agnostic model without
changing its intended matching logic.

Each rule shows **Matching active ads** directly within its destination channel.
Only successfully AI-processed messages whose `ai_category` equals the rule category,
with an extracted record in that category, are included. This excludes unprocessed,
failed, and cross-category records. The list follows the rule's source-channel and
AND/OR field conditions, supports pagination (20 at a time), and is refreshed when
the admin reloads the page. Use **All**, **Unsent**, **Delivered**, and **Needs review**
to filter matches for that individual destination. **Unsent** means no recorded
delivery or an explicit retry; **Needs review** covers uncertain, rejected,
failed, and sending states. Statuses are destination-specific and the counters are
computed over all matches, not just the current page.

Here, "active" means successfully extracted and eligible for rule matching, **not**
that the external Telegram post or listing is guaranteed to remain available:
the database currently has no universal expiration or deactivation flag. A
category-specific expiration policy would need a separate data model.

New rules default to **Automatic publishing**; select **Manual review** to send
individual matching ads with **Send now**. Existing rules keep their stored
mode. Already delivered ads show **Send again**; resending requires confirmation
and preserves the original successful delivery record.

**Delete rule** removes only the rule, leaving past delivery history intact. Rules run in
priority order. The first matching rule stops evaluation by default; enable **Continue
after match** to allow later matching rules to publish to additional channels/groups.
Repeated matches to the same channel/group are deduplicated.

If Telegram returns HTTP 429, publication pauses for the `retry_after` time returned by
Telegram, including after process restarts. If a send is interrupted, it becomes
**uncertain** after five minutes and requires manual review before retrying.

With no matching rule, a message is not published. Each message and publishing
channel/group has its own delivery record, and successful automatic sends are not repeated
for that same channel/group.

### Import structured website listings

Open **Database → Import CSV** (`/data/import`). Jobs (`joblist`), Housing
(`housinglist`) and Cargo (`transferlist`) each have a separate card, template,
file chooser and upload button. Download the template from the matching card
and upload one category per UTF-8 CSV (up to 512 KiB, 500 rows). Review the first
five rows and the destination category, then use that category’s save button. Every row
is validated before any write. A file with invalid data is rejected entirely;
a database failure rolls the whole file back. The preview itself writes nothing.

Required columns: `source_url` (one public HTTP/HTTPS listing URL per row) and
`description` (source listing text). Optional `source_date` is the original ISO
date/datetime, never replaced with today's date; `raw_text` preserves original
source evidence. All other columns use the selected category's template names.
Empty cells and `NULL` become SQL NULL. Numeric columns require finite numbers;
Jobs `remote` and `is_job_listing` accept true/false or 1/0. Unknown columns,
including Telegram IDs and local database IDs, are rejected. Import does not
infer locations, verify employment, or call AI or fetch URLs.

Imports are category records with **NULL processed_message_id**, with no invented
Telegram message, channel, sender, or user. Provenance is recorded in read-only
`external_category_sources`, including category, category_record_id, source URL,
original date/text, importing admin and import timestamp. AI database analysis
can query that table and join `category_record_id` to the selected category's
`id`, also filtering `category` when joining. The import result links to each
category row in the editor. Filter `processed_message_id IS NULL` to find imports.

A repeat URL within the same category (ignoring URL fragments) is skipped and
never overwrites previous data, including repeats within a file. This is URL
identity checking, not content duplicate detection across different websites.

**Import is storage only, not publication.** Existing Telegram delivery queues
require a real parent message and do not admit these records. Publishing website
imports requires a separately designed admission path with source freshness,
validation, contact and duplicate checks. No such publication path is enabled
by importing a file. Success reports saved/skipped counts, not sent counts.

On first startup, existing category tables are migrated transactionally to
allow NULL Telegram parents. Existing IDs, links, values and creation timestamps
are retained; non-NULL parent uniqueness, foreign keys and cascade deletion stay
in force. As with other schema changes, retain the deployment's SQLite backup.

### Shared back-office appearance

All nine authenticated sections use `backoffice_layout.py` for the same page
width, typography, background, form controls, header and navigation. Render
`SHARED_STYLE` after page-specific styles and `page_header(active, title)` at the
start of the body. The header stays left-to-right across sections; Persian
Import CSV and AI Assistant content uses a right-to-left content container.
The active section has `aria-current="page"`, keyboard focus is visible, and
navigation wraps on narrow screens. Keep page-specific layouts scoped so they
do not replace the shared frame. Import preview, validation errors and saved-row
results all keep the Import CSV frame.

Category-specific uploads POST to `/data/import/{category}`. Both preview and
save stay bound to that destination; a conflicting form category is rejected.
Columns from another category are rejected even if their cells are empty. An
optional `category` CSV column must equal the destination table name in every
row; missing or different values reject the complete file. Without this column,
all rows belong to the card used for uploading. This validates file structure
and declared category, not semantic content: it does not run AI to classify
free text. Templates contain only the matching category's columns and shared
source metadata. The previous POST endpoint remains compatible, using the same
validation and category-specific preview/save flow.
