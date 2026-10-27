# Advertio Manual Delivery

## Purpose

The main TUI can send already-processed housing listings to Advertio without starting a new Telegram crawl or running Groq again.

## Eligibility

A listing is eligible when all of the following are true:

- `messages.processing_status = 'processed'`
- `messages.ai_status = 'processed'`
- `messages.ai_category = 'housinglist'`
- a corresponding `housinglist` category row exists
- `advertio_status` is `waiting` or `retry`

`rejected` records are not automatically retried because Advertio `400` responses are permanent according to the Advertio contract.

## Flow

```text
Existing SQLite data
       ↓
Eligible housing query
       ↓
TUI: Send eligible ads to Advertio
       ↓
AdvertioDeliveryService.deliver_pending()
       ↓
Existing housing data
       ↓
If media_type=photo:
reuse valid local media OR lazily re-download from Telegram
       ↓
Advertio media upload
       ↓
POST /api/ingest/leads
       ↓
Update advertio_status
```

No crawler and no AI extraction are involved in this flow. A Telegram client may still be used to lazily restore photo media for a waiting/retry record whose local files are absent. This is a media fetch only; it does not re-crawl or re-run AI.

## Main Menu

The option is:

```text
4. 📤 Send eligible ads to Advertio
```

The user can select how many existing eligible records to send. Progress and final counts are shown in the terminal.

## Status handling

- successful new lead → `sent`
- Advertio idempotency response (`alreadyExisted=true`) → `already_existed`
- missing/stale local photo with no usable downloader → `retry`; the lead request is not sent
- Telegram photo download failure/empty result → `retry`; the lead request is not sent
- retryable network/5xx error → `retry`
- permanent mapping/400/other non-retryable error → `rejected`

The record's `advertio_lead_id`, `advertio_error`, and `advertio_processed_at` are updated together with the status.

## Important design rule

This manual menu is a delivery operation only. It must not trigger:

- Telegram crawling
- information processing
- duplicate detection
- Groq extraction
- re-generation of the AI result

The source of truth for the Advertio payload is the existing persisted `housinglist` record created during AI processing.

Photo delivery is fail-closed: when a record says `media_type=photo`, Advertio must not receive that lead with an empty `mediaKeys` array merely because the local cache is missing. Media must first be restored successfully or the record remains retryable.

### Housing Retry-After

Housing persists its API cooldown in `advertio_housing_cooldown`. A retryable
Advertio error with a positive `Retry-After` pauses all Housing sends until that
UTC time; the numeric and HTTP-date header formats are parsed by the shared
client. A 429 with no usable positive delay defaults to 60 seconds. A later,
shorter delay cannot replace an existing longer pause. Other errors without a
server wait retain their existing retry/permanent-failure behavior.

The same guard applies to manual/scheduled `deliver_pending`, inline Housing
publication after AI extraction, media uploads and direct service delivery.
The first limited row retains retry status and media; untouched rows remain
waiting. During the pause, queue results include `paused_until`, and inline AI
reports skipped/no delivery attempt while retaining successful extraction.
The cooldown survives process restart and is checked before the queue resumes;
no blocking sleep is added. A process-local send lock prevents concurrent Housing
service instances from starting a new send before the first response's pause
has been saved. Requests already in flight in another process cannot be recalled.
Jobs/Cargo policy, source identity, terminal statuses and API payloads are unchanged.
The cooldown table is created only when a qualifying wait must be saved; normal
checks do not migrate the database or require a full reinitialization.
