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
Advertio media upload (if media exists)
       ↓
POST /api/ingest/leads
       ↓
Update advertio_status
```

No crawler and no AI extraction are involved in this flow.

## Main Menu

The option is:

```text
4. 📤 Send eligible ads to Advertio
```

The user can select how many existing eligible records to send. Progress and final counts are shown in the terminal.

## Status handling

- successful new lead → `sent`
- Advertio idempotency response (`alreadyExisted=true`) → `already_existed`
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
