# Advertio Jobs delivery

Implementation branch: `feat/advertio-jobs-auto-ingest`, based on maintained default
`main` at `7bfa9358f267944201e2a2799a6f2764009256b4` (includes merged provider fixes and
startup performance fixes). `Stage` was inspected; its four divergent commits are
AI fallback diagnostics/tests, not a different Jobs integration. This change does
not merge branches or deploy the running collector.

## Verified receiving contract

Inspected GitLab `advertio/platform/advertio-monorepo` default `master` at
`0d84b241784f601cf4a2aa8a3be1467530cb19d0`: ingest endpoint, command and handler,
category seed, attribute validator, Jobs form/title builder, location endpoints,
and `docs/CRAWLER-INTEGRATION.md`. Also read the four requested documents in the
GitHub Advertio documentation repository. Live read-only verification on
2026-10-25 returned HTTP 200 for `/health` (Healthy), Jobs attributes, countries,
Canadian provinces and Toronto city search. The live schema is captured in
`tests/fixtures/advertio_jobs_schema.json`; tests never fetch production data.

The receiving API needs **no backend changes**. Its active schema is authoritative.
Older advice to start every category with `autoPublish=false` is superseded **for
eligible Jobs** by the explicit product decision in this task. Housing and Cargo
flags are unchanged. The approximate 1,000 shared daily request budget is a local
initial ceiling, **not a verified production capacity or current edge policy**.
Confirm that policy with the Advertio operator before rollout.

## Architecture and independence

Collection → established cleaning → classification → category extraction → existing
`joblist` persistence → dedicated Jobs outbox → existing `AdvertioClient` → ingest
validation/dedup/moderation → persisted delivery result and actual remote status.

- `delivery/advertio_jobs.py`: catalog cache, mapping, safety checks, queue worker.
- `storage/advertio_jobs_queue.py`: additive startup schema and deployment watermark.
- `delivery/advertio_transport.py`: shared semaphore and atomic daily HTTP reservations.
- `backoffice_jobs.py`: read-only diagnostics and operations section.
- Scheduler runs Jobs after existing Housing/Cargo stages via `asyncio.to_thread`,
  under the existing pipeline lock. A database lease additionally prevents overlapping
  Jobs workers across manual operations, scheduler instances and processes sharing DB.
- AI extraction succeeds independently of delivery. No Jobs response updates
  `messages.advertio_status`, Cargo tables or Telegram publication status.

No HTTP or schema initialization runs on Back Office page reads. Catalog calls
are public and never include the ingest key. HTTP errors persist fixed codes,
not remote response bodies, applicant information or credentials. Redirects are
disabled on AdvertioClient requests so a custom ingest header cannot follow a
redirect to another host.

## Extraction and compatibility

The Jobs allow-list/prompt now includes `role`, `job_category`, canonical `job_type`,
`job_title`, `country_code`, `province`, `city`, `work_arrangement`, `salary`,
`salary_currency`, `salary_period`, `description`, and `is_job_listing`.
`is_job_listing` explicitly distinguishes employment from service promotions/spam.
Unknown facts remain null. A full-time value additionally needs full-time wording
in cleaned source text; equivalent English and Persian phrases are supported.

Existing `employment_type`, company, skills, experience, education, location,
remote, application method and contact remain in storage. Historical `job_type`
values are **not rewritten**. Legacy records missing the new eligibility evidence
cannot auto-publish; correct/re-extract them explicitly first. Jobs extraction defaults an unspecified salary currency to CAD by explicit product
decision. Explicit foreign compensation remains stored internally and in the original
description; it is not converted or submitted as CAD.

`messages.sender_is_original` is nullable. Newly crawled unforwarded messages get
1; forwards get 0; existing unknown provenance stays null. A verified human
original sender can supply their handle and positive user ID. Unrelated handles
in message text, source channel usernames and chat/message IDs are not contacts.
Otherwise use the actual stored post URL, or construct its canonical public URL
from the known source username and original message ID (needed for legacy
text-only collector rows). Private `/c/` links must match the stored channel ID.

## Mandatory attributes and taxonomy

`attributesJson` is serialized once as a JSON **string**. Required values:

| Attribute | Values |
| --- | --- |
| role | employer, seeker |
| job_type | full_time, part_time, contract, temporary |
| job_category | restaurant_food, construction, cleaning, delivery_driver, retail, beauty_personal_care, office_admin, it_tech, general_labour, warehouse, skilled_trades, customer_service, sales, healthcare_support, childcare, education, design_creative, other |

The worker checks live attribute types/options before transmission and pauses on
schema drift. `other` requires an identified occupation; it is not an uncertainty
fallback. Optional `work_arrangement` is on_site/hybrid/remote. Remote is never
`job_type` and still needs an actual Canadian city. Optional title uses a stated
position of at most 60 characters; otherwise the English category label from the
live schema is used. Generated listing title:
`Hiring|Looking for work · position or category · canonical city`.

Only current keys are emitted: role, job_category, job_type, job_title,
work_arrangement, salary, salary_period. Legacy/internal fields never leak into
`attributesJson`. Their factual content is retained through the original description.

## Geography

Only `countryCode=CA` is accepted locally. The worker caches the live schema,
countries, provinces and exact city searches for one hour, with bounded cache size.
It calls `cities?countryCode=CA&q=...&provinceCode=...&limit=200` where province is
available. It does **not** use the first 200 nationwide cities as the entire catalog.
A saturated search result is held, because the current API exposes no pagination.

Code, English name and Persian `nameFa` must match exactly after case/whitespace and
Persian character normalization. Source text must contain that city's name/code or
catalog Persian alias. An omitted province may be derived from a **unique matching
catalog city**, never guessed. Explicit conflicting/unknown province, ambiguous city,
unknown city, nearby-city substitution and location-free remote work are blocked.
No neighborhood is substituted for a different catalog city.

## Compensation and description

Salary extraction is AI-led: the prompt asks for the amount (lower end of a range),
actual payment period where identifiable, and currency. Unspecified currency defaults
to CAD by explicit product decision; an explicit other currency is preserved without
conversion. The adapter no longer reparses the source with restrictive salary regexes.
It trusts extraction while still enforcing finite numeric amounts and the active API
bounds (currently 1–1,000,000). A salary and the word negotiable may coexist: keep the
amount. Unknown payment periods are omitted; the active backend permits salary without
salary_period, and the frontend can display the CAD amount without a period suffix.

When AI extracts no salary, the outgoing description gains the display-only footer
`حقوق: توافقی (Negotiable)`, unless the original already says negotiable/توافقی/قابل مذاکره.
The original source text is retained verbatim as the prefix and is never overwritten
in SQLite. This makes the fallback visible in listing details using today's contract:
**there is no new structured salary_negotiable attribute or feed-card Negotiable badge**.
A dedicated badge/filter requires an Advertio change; the current API rejects that key.
Explicit foreign amounts and amounts outside API bounds remain in the original text
without a false negotiable label. They are not submitted as CAD numeric attributes.

No AI summary replaces the cleaned description. Established cleaning is unchanged.
Description must have at least 20 nonblank characters. Both the original and the final
description with any footer must fit 2,000 UTF-16 code units; an over-limit record is
blocked, never truncated. Title still respects the 200-character envelope limit.

## Example request

Authentication: `X-Ingest-Key` from server configuration; never paste it into UI/logs.

```json
{
  "sourceName": "tg-jobs-0123456789abcdef",
  "externalId": "512",
  "sourceUrl": "https://t.me/reviewed_jobs/512",
  "contactHandle": null,
  "title": "Hiring · Line cook · Toronto",
  "description": "Hiring a part-time line cook in Toronto. CAD 20/hour. Apply through the original Telegram post.",
  "categorySlug": "jobs",
  "attributesJson": "{\"role\":\"employer\",\"job_category\":\"restaurant_food\",\"job_type\":\"part_time\",\"job_title\":\"Line cook\",\"salary\":20,\"salary_period\":\"hour\"}",
  "countryCode": "CA",
  "province": "ON",
  "city": "toronto",
  "mediaKeys": [],
  "autoPublish": true
}
```

Poster user ID is optional and emitted only for a proven original human author.
There is no call to the native `POST /api/leads` endpoint.

## Automatic publishing and safety

Eligible new records on explicitly reviewed sources request `autoPublish=true` by
default. The Jobs, parent ingest and API-key gates all apply inside the worker,
including manual batches. Empty source approval list blocks every Jobs record.
Source approval is distinct from classification and mandatory completeness checks.

Conservative source-text rules flag application/recruitment/training/equipment fees,
suspicious crypto/gift-card/check transfers, banking-password requests and identity
harvesting. Flags are explainable fixed labels. Valid flagged payloads request
`autoPublish=false`; incomplete flagged records stay blocked. These rules are a
limited heuristic, not comprehensive fraud detection; false positives go to moderation.

The receiver alone decides publication. A Similar verdict remains `PendingReview`
even with autoPublish true. Store the actual response unchanged. A sent or
already_existed row is terminal and cannot be requeued through the UI. No extra
request is made to force it Active. Status counts are the last **recorded** remote
response, not ongoing synchronization with subsequent moderator actions.

## Schema migration and queue states

Startup `MessageRepository.initialize()` invokes existing DB initialization once
per database/process. Nullable additions: six TEXT columns (role, job_category,
country_code, province, city, work_arrangement), INTEGER `joblist.is_job_listing`,
and INTEGER `messages.sender_is_original`. No rename, record deletion, table-wide
Jobs rewrite or per-channel schema initialization is introduced.

New tables: `advertio_jobs_deliveries` (FK to messages.id), `advertio_jobs_attempts`
(FK to messages.id), `advertio_jobs_meta`, and `advertio_request_log`.
Outbox persists frozen source identity, lead ID, remote status, fixed error code,
safety flags, validation timestamp, evaluation attempts, retry time, first/last
HTTP attempt times, update time and worker claim token. Attempts history records
HTTP start/outcome; interrupted attempts are marked abandoned on recovery.

| State | Meaning |
| --- | --- |
| waiting | Discovered post-deployment record; still needs full validation |
| sending | Claimed by one leased Jobs worker |
| sent | API accepted; inspect actual remote_status separately |
| already_existed | Successful terminal no-op, including cross-source duplicate |
| retry | Temporary failure/cooldown; unchanged source identity |
| rejected | Permanent HTTP/schema rejection requiring correction |
| blocked | Local ineligibility or exhausted retry allowance; explicit reevaluation |

Evaluation attempts are bounded (default 8); HTTP attempts are separately audited.
The worker renews its lease between rows. Abandoned sending claims recover after
the larger of configured claim timeout and `12 × HTTP timeout + 120` seconds.
Scheduler cutoff defers newly failed/recovered retry rows until a later cycle.

Source namespace: `tg-jobs-` + first 16 SHA256 hex characters of numeric channel ID
as stored by the collector. If unavailable, hash the trimmed lowercase username
without `@`. The original Telegram message ID is externalId. Freeze both values
when first enqueued, including across retries and corrected fields. Do not change
this convention in production. Username-only sources that rename require an
explicit identity migration; do not delete/recreate their queue rows.

## HTTP outcomes, budgets and operator controls

- 201: save lead ID and actual status (Active or PendingReview).
- 200/alreadyExisted: terminal success. It may reference another source's lead;
  do not assume a current-origin mapping was created or delete that other lead.
- 400/other permanent errors: rejected, no automatic resend.
- 401/404 ingest: retry row, persistent queue fault and operator activity alert;
  **explicit resume is required even after the displayed cooldown timestamp**.
- Schema drift: pause and operator alert; update mapping after inspection, then resume.
- 429: respect numeric or HTTP-date Retry-After; wait at least that long.
- 5xx, malformed success response, network timeout: bounded exponential retry with
  fixed source identity. A timeout can occur after creation; it never proves absence.
- Unexpected failures: sanitized retry and 10-minute queue cooldown.

Jobs HTTP request ceiling defaults to 200/day, batch to 25 evaluations. A shared
AdvertioClient request ledger reserves daily capacity atomically before I/O for
Housing, Cargo and Jobs; media and catalogs count too. A shared semaphore caps
process HTTP concurrency at existing ADVERTIO_CONCURRENCY. Requests made outside
AdvertioClient (for example the Back Office health probe) are not in that ledger.
Daily boundaries are UTC. Receiver-side limits can still be lower and return 429.
Jobs can use fewer than 200 ingests because its public catalog requests count.

Back Office → Operations → **Advertio Jobs** shows gates/key presence, AI counts,
all queue counts, cached eligible vs unassessed records, actual recorded remote
Active/PendingReview counts, request budget, cooldown/faults and recent history.
It provides Send next eligible Jobs, safe stop after in-flight calls, corrected
blocked/rejected requeue, explicit bounded backfill when enabled, and API-fault
resume. Manual delivery uses the same service/lease/validation/budget. Settings has
an independent Advertio Jobs group, including source approvals and auto-publish.
The existing authentication/CSRF requirements apply to these operations.

## Environment defaults

| Variable (TELCLAW_ prefix) | Default |
| --- | --- |
| ADVERTIO_JOBS_ENABLED | false |
| ADVERTIO_JOBS_AUTO_PUBLISH | true |
| ADVERTIO_JOBS_BACKFILL_ENABLED | false |
| ADVERTIO_JOBS_BACKFILL_DAYS | 7 |
| ADVERTIO_JOBS_FRESHNESS_DAYS | 7 |
| ADVERTIO_JOBS_BATCH_SIZE | 25 |
| ADVERTIO_JOBS_DAILY_LIMIT | 200 |
| ADVERTIO_JOBS_MAX_ATTEMPTS | 8 |
| ADVERTIO_JOBS_CLAIM_TIMEOUT_SECONDS | 1800 |
| ADVERTIO_JOBS_SOURCES | empty, comma-separated reviewed numeric IDs/usernames |
| ADVERTIO_DAILY_REQUEST_LIMIT | 1000 shared HTTP requests/day |

Also requires the existing TELCLAW_ADVERTIO_INGEST_ENABLED=true and configured
TELCLAW_ADVERTIO_INGEST_KEY. Back Office persisted settings override environment
values: inspect/reset any override before changing `.env` during deployment/rollback.
No Housing/Cargo flags need to change.

## Backfill, deployment and rollback

1. Review the PR and run `python -m pytest -q tests` in the project environment.
2. Back up SQLite with its backup API (or stop application and copy DB plus WAL).
3. Deploy the feature commit with Jobs **disabled**; restart normally so startup
   applies additive schema. Record the new deployment watermark.
4. Confirm `/health`, live Jobs schema and location search again from the actual
   deployment network; confirm current shared rate policy with Advertio operations.
5. Confirm ingest key presence on the server without printing its value. Add only
   reviewed channel IDs to ADVERTIO_JOBS_SOURCES. Keep auto-publish true.
6. Temporarily set batch size 1–5 and a small daily request limit sufficient for
   schema/catalog lookup as well as the ingest. Explicitly enable Jobs.
7. Collect a genuinely fresh, complete job advertisement from the reviewed source;
   use Send next eligible Jobs. Inspect source URL/contact, actual lead ID and status.
   Verify an actual unique listing becomes **Active** without manual approval.
8. Verify incomplete jobs remain blocked, scam-flagged jobs request moderation,
   duplicates stop, and Similar retains the receiver's PendingReview verdict.
9. Restore batch 25/daily 200, retain backfill false; let normal scheduled cycles run.

First schema installation stores MAX(messages.id) as a fixed boundary even while
disabled. Ordinary discovery excludes all records at/below it. It admits only
post-deployment, recently dated source messages; reprocessing an old SQLite record
alone does not make it new. Enabling BACKFILL_ENABLED alone never scans history.
An explicit Back Office backfill admits at most the selected batch within the
smaller of backfill/freshness windows. Verify that historical jobs remain open
before invoking it; their existence in SQLite is insufficient evidence.

Rollback: set **effective** TELCLAW_ADVERTIO_JOBS_ENABLED=false (including persisted
Back Office overrides). The worker checks this between requests; an in-flight call
may finish and is recorded. Stop Jobs Delivery can stop the current batch without
a code rollback. Keep additive tables/columns and successful records; disablement
never deletes/deactivates published listings and does not alter Housing/Cargo or
Telegram delivery. Keep a database backup before any separate code rollback.

## Known operational limits and release gates

- Live health/schema were verified; authenticated ingest, public source-link opening,
  live automatic Active publication and moderation scenarios **have not been verified
  in production by this change**. No ingest key or deployment-server access was
  supplied in this development environment. Unit tests use controlled HTTP fakes.
  Those staged rollout checks remain required before calling the release deployed.
- Telclaw has no reliable Telegram delete/withdrawal event reconciliation. Therefore
  this feature performs **no automatic deletion or source deactivation**. A crawl miss,
  partial scan, AI failure or disconnection never issues DELETE. Full deletion sync
  is not claimed. A future verified event reconciler must call only the per-post
  `DELETE /api/ingest/leads/{sourceName}/{externalId}` for the positively confirmed
  original post; cross-source duplicates may have no mapping and return 404.
- No Telegram edit/upsert or later remote moderation-status polling is introduced.
- Scam rules and supported employment phrases are deliberately conservative. Salary
  semantics are delegated to AI and can be wrong; numerical API validation is not a
  guarantee of extraction accuracy. Incorrect values need correction/re-extraction.
- Catalog values, source approvals, key validity and active job availability remain
  operational inputs, not facts inferred from a successful SQLite lookup.

## Development validation and exact file manifest

Verified on the feature branch with Python 3.12:

- `python -m pytest -q tests`: **1,074 passed**, 56 existing deprecation/aiohttp warnings.
- Dedicated Jobs coverage: **96 passed**, including real AI-service persistence to the
  outbox, both roles, missing fields, multilingual payloads, compensation, unsafe
  moderation, remote statuses, retries, concurrency, recovery, settings gates,
  explicit backfill and shared budgets. All ingest HTTP calls are mocked.
- Ruff undefined-name/import checks on new modules, Jobs tests and changed client: passed.
- `git diff --check`: passed.
- Production read-only health/schema/catalog checks: HTTP 200; no authenticated
  ingestion or deployment was performed.

Modified files:

```text
.env.example
ai/category_classifier.py
ai/category_schemas.py
ai/extractor.py
ai/prompts/joblist.txt
backoffice_health.py
backoffice_operations.py
backoffice_settings.py
backoffice_web.py
collection/crawler.py
config.py
delivery/advertio_client.py
docs/SYSTEM_STRUCTURE_MAP.md
services/scheduler_service.py
storage/database.py
```

New files:

```text
backoffice_jobs.py
delivery/advertio_jobs.py
delivery/advertio_transport.py
docs/ADVERTIO_JOBS_DELIVERY.md
storage/advertio_jobs_queue.py
tests/fixtures/advertio_jobs_schema.json
tests/test_advertio_jobs.py
```

## Recover Jobs skipped while AI extraction was disabled

In Back Office → Operations → AI Extraction, enable Jobs extraction in Settings,
then use **Requeue Jobs skipped while AI was disabled** (default 100, maximum
1000 per action). The confirmation and result notice show the scope and affected
count. Next select a Telegram account and use **Run AI Extraction**; the scheduler
may also process the pending queue normally.

Only processed/classified Jobs with `ai_status=skipped` and the exact reason
`skipped:category_disabled:joblist` qualify. Existing extracted Jobs and any Jobs
with delivery queue records are excluded. Other categories, successful/failed AI
outcomes, Telegram publication and Advertio delivery states remain untouched.
This action only requeues extraction; it neither calls AI nor ingests listings.
Historical delivery still requires the separate explicit bounded Backfill.
No migration or automatic historical requeue runs on startup.

## Operations workspace navigation

Operations now has a queue overview and separate navigation for Processing & AI,
Jobs, Housing, Cargo and management tools. Pick Jobs to see its independent queue.
Recovery/backfill and record history expand on demand; primary delivery controls
stay visible. AI controls explain why recovery can be unavailable. Results show
key counters first with raw JSON under technical details. Selecting Jobs does
not send anything or change delivery settings.

The selected section and opened details persist in the browser tab across action
redirects and polling refreshes. Running operations refresh every 10 seconds;
editing a form pauses polling until the next navigation/reload. With JavaScript
disabled all sections remain visible and anchor navigation/forms still work.
No database migration, API contract, queue selection or publication policy changes.

## Diagnose successful AI extraction with no Jobs transmission

A manual AI extraction result is not a Jobs delivery report. The extraction
stage's legacy `advertio` counters belong to Housing. Jobs delivery uses its own
queue, manual command and scheduled stage.

Jobs now includes **Why are Jobs not being sent?** with mutually exclusive counts
for normal admission versus an explicit backfill preview: awaiting successful
Jobs extraction, successful flag without a stored Jobs row, incomplete processing,
already queued, invalid/future source date, outside the effective date window,
pre-deployment records needing explicit backfill, and ready for queue admission.
The preview performs local SELECT queries only, does not call AI/Advertio, and
does not validate eligibility or send anything. Already queued records must be
inspected by delivery state; blocked/rejected records are not silently requeued.

`enqueue()` returns the admission snapshot plus the actual inserted count.
Delivery results retain it under `discovery`; manual historical runs additionally
retain `backfill_discovery` so the following normal discovery does not conceal
the historical admission reason. Cooldown, disabled, busy and exhausted-budget
results remain distinguishable from an empty queue.

Windows use the original Telegram date, not `ai_processed_at`. Explicit backfill
uses `min(ADVERTIO_JOBS_BACKFILL_DAYS, ADVERTIO_JOBS_FRESHNESS_DAYS)`. Merely enabling
backfill does not queue history. Check the source job is still open before a
bounded backfill; do not refresh the source date or reset the deployment watermark
to manufacture freshness. After deploying, refresh Operations → Jobs and inspect
both admission columns before retrying. Invalid dates require source correction;
missing extraction requires AI processing; historical rows inside the permitted
window require explicit backfill. Ready for admission is not proof that role,
job type, location, source allowlist or scam checks will pass.

Regression tests reproduce old AI-processed Jobs excluded from normal delivery,
then explicitly backfill a recent historical record, verify autoPublish=true and
terminal idempotency. Other tests cover stale/invalid dates, effective window,
failed extraction/missing category data and read-only preview. No production DB
was accessed, so a particular operator's empty queue still requires its admission
counts for diagnosis. No migration or environment changes are needed.

## Recover successful AI flags with missing Jobs data

If admission reports `missing_extracted_record`, `messages` says extraction
succeeded but the related `joblist` row is absent. Do not construct a listing
from that flag or guess the missing data. This symptom alone cannot identify
whether legacy data, a prior migration or another write path caused the mismatch.

Operations → Jobs now offers **Recover missing Jobs extraction data (count)**.
After confirmation it requeues at most the requested 1–1000 records (default 100),
newest first. It requires enabled Jobs AI and an idle manual pipeline, holds the
shared pipeline lock and logs the affected count. Only successful Jobs rows with
processed text/classification and no Jobs data or delivery queue entry qualify.
It preserves source date/text, category, Telegram status, valid Jobs records and
all existing delivery states. Nothing runs automatically on startup.

Then use **Run AI Extraction**, inspect admission again and explicitly backfill
still-open historical jobs inside the permitted date window. Recovering the
missing row does not make an old job current. The awaiting-AI diagnostic also
breaks down its total into stored statuses, distinguishing pending, skipped and
failed rows. Skipped-while-disabled recovery and failed-extraction retry remain
separate operations. No migrations or new environment variables; deploy/restart
the feature commit to expose the new control. Rollback by reverting the code;
records already requeued remain pending and can be processed normally.
