# Advertio passenger-cargo ingestion (Telclaw)

Status: implemented in Telclaw. This document describes the dedicated API
destination. It does **not** replace the established Telegram `@advertio_cargo`
publisher or the housing ingestion adapter.

Platform contract: [Advertio crawler integration](https://gitlab.com/advertio/platform/advertio-monorepo/-/blob/master/docs/CRAWLER-INTEGRATION.md).
Cargo category: `passenger-cargo`. One pool; `role` is intentionally omitted.
As of 2026-10-21, `travel_date` is optional.

## Deployment requirements

1. Verify `https://api.advertio.ir/health` is reachable with valid TLS.
   The platform guide notes that the dedicated API hostname was not yet live
   when the contract was updated on 2026-10-21. Do not assume it is available.
2. Configure `TELCLAW_ADVERTIO_INGEST_KEY` with the same secret as the API
   server's `INGEST_API_KEY` (minimum 32 characters). Do not commit secrets.
3. Set `TELCLAW_ADVERTIO_INGEST_ENABLED=true`. Cargo is on by default only
   when this parent switch is on; set `TELCLAW_ADVERTIO_CARGO_ENABLED=false`
   to turn off cargo alone without affecting Housing or Telegram.
4. `TELCLAW_ADVERTIO_CARGO_AUTO_PUBLISH=true` by default for cargo only.
   Only unique, API-validated new cargo leads become Active. Similar leads remain
   PendingReview. Existing PendingReview records **do not** become Active
   merely because this setting changed. Disable this flag for any untrusted source. This feature flag is **independent** of the
   housing `TELCLAW_ADVERTIO_AUTO_PUBLISH` switch.

### Cargo environment settings

| Setting | Default | Meaning |
|---|---|---|
| `TELCLAW_ADVERTIO_CARGO_ENABLED` | `true` | Enabled if parent ingestion is enabled |
| `TELCLAW_ADVERTIO_CARGO_AUTO_PUBLISH` | `true` | Automatically publish newly ingested, validated, unique cargo only; source-specific caution required |
| `TELCLAW_ADVERTIO_CARGO_BACKFILL_DAYS` | `30` | Undated historical posts must be this recent |
| `TELCLAW_ADVERTIO_CARGO_BATCH_SIZE` | `50` | Maximum records examined per scheduled cycle, capped at 100 |
| `TELCLAW_ADVERTIO_CARGO_DAILY_LIMIT` | `250` | Local cap on cargo ingest POST attempts (maximum configurable 500) |

The platform's ~1,000-request/day shared Cloudflare cap also includes housing,
jobs and uploads. `250` is a conservative *cargo-only* limit, not a global
budget guarantee. Tune the limit with the platform operators if housing
consumes significant capacity.

## Data flow

```text
Telegram crawler -> messages -> classification -> AI extraction -> transferlist
                                              |
                          +-------------------+----------------------+
                          |                                          |
                  Telegram publisher                         Advertio cargo queue
                  existing rules                             category passenger-cargo
                          |                                          |
                  @advertio_cargo                          POST /api/ingest/leads
```

Cargo never reuses `messages.advertio_status` (housing's status column).
It persists API state under:

- `advertio_cargo_deliveries`: one row per internal SQLite
  `messages.id`, state `waiting / sending / sent / already_existed /
  retry / rejected`, attempt count, error, remote `lead_id`,
  `next_attempt_at` and timestamps.
- `advertio_cargo_attempts`: append-only HTTP attempt timestamps for
  daily budget accounting.

The tables are created idempotently at cargo service startup.
Abandoned `sending` rows older than 30 minutes are reclaimed as `retry`.
HTTP 429, 401, 404 and transient failures trigger global cooldown; 400 mapping
errors are terminal until an operator corrects the source data. A successful
`alreadyExisted` response is terminal and must not trigger retry.

The unique remote origin uses `sourceName=tg-cargo-<SHA256(channel ID or
normalized username)[:16]>` and `externalId=Telegram message ID`.
Therefore message ID 4021 in two different channels creates distinct
idempotency identities. **Never change this convention for sources that
have already been ingested**, or duplicates may be created.

## Validity and historical backfill

Backfill is automatic, not a separate crawl. All previously AI-processed
`transferlist` rows that have not succeeded or been permanently rejected
are considered under the following rules:

- A future (or today's) explicit `departure_date` may qualify regardless
  of source-post age.
- A record without an explicit date qualifies only when its Telegram
  message date is within the configured `BACKFILL_DAYS` window.
- Past-dated trips are excluded **before** batch selection (including older
  trips captured again during a new crawl); their historical rows remain intact.
- Records with incomplete cities or countries outside the platform's seven
  supported country codes are also excluded from batch selection, without
  deleting or marking the original source record as rejected.
- The manual/scheduled worker first prioritizes the closest upcoming travel
  dates, then the newest undated messages, so small batches can make progress.
- The Back Office's **Ready to send now** count is the exact local queue
  eligibility count; **Waiting** is a broader historical/date-window count.
  Final city-catalog and content validity still require an API attempt.
- Both countries and both cities must be known and present in Advertio's
  live location catalog. Missing/unsupported locations are not guessed.
- `description` is the already-extracted, cleaned post text; no Telegram
  publication formatting is reused.
- Poster Telegram ID is sent only for `sender_type=user`. The sender
  channel's handle is **never** sent as the claimed poster's handle.
- `mediaKeys` is empty; cargo has no media slot.
- `cargo_type` is sent as an array of supported type codes.
- Weight is sent only when it can be safely converted to 0.1–30 kg.
- A numeric price is sent only when the **original source text** clearly
  states a per-kg price in CAD, USD, EUR or GBP. Flat fees and unsupported
  currencies remain in the description.

**Meaning of "still active".** The database contains the message capture
date and sometimes a future trip date; it does not reliably know whether
the original Telegram post was later deleted. Therefore backfill uses
expiry/freshness *eligibility*, not a guarantee of live source existence.
If strict real-time verification of each historical post is required, a
Telegram reconciliation process must be added before broad backfill.
Never call source-wide DELETE based only on a failed crawl or a missing
message in an incomplete scan.

## When a fresh crawl does not appear on Advertio

Telclaw has four separate states; a completed crawl is **not** successful API
publication:

1. Collection and classification must identify a `transferlist` message.
   Failed Cloudflare extraction remains in the **AI Extraction Queue** and
   cannot reach Advertio; use "Retry Invalid AI Extraction" as appropriate.
2. The post must pass the dedicated cargo eligibility window (future/current
   departure or a recent undated source), source URL/contact, city catalog and
   required origin/destination checks. Expired or stale records are excluded.
3. `TELCLAW_ADVERTIO_INGEST_ENABLED`, the cargo switch and the ingest key
   must all be configured, and the API must be reachable. Network errors,
   invalid keys, rate limiting and schema errors are shown in the cargo queue.
4. `autoPublish=true` publishes new unique cargo leads immediately; records
   flagged Similar still require moderation. Existing PendingReview leads
   remain pending until a separate backend approval flow handles them safely.

### Active Cargo report (after deployment)

- Open **Back Office → Active Cargo** (/cargo) for the date-active candidate
  pool. Use source/city/Telegram ID search, API status and trip-date filters,
  and pagination to inspect historic and recently crawled cargo records.
- The report distinguishes **Active candidates** (date/freshness only) from
  **Ready for API** (local country, city completeness, waiting/retry state and
  backoff window). It cannot verify Telegram deletion or Advertio visibility.
- Mark checkboxes beside ready ads and choose **Send selected active cargo to
  Advertio** to submit precisely those internal IDs through the normal
  idempotent queue, or **Send next 25 ready ads** to drain the highest-priority
  part of the queue automatically. Batch and daily limits still apply.
- Source messages and Housing/Telegram delivery states remain unchanged.

### From Telclaw Back Office (after deployment)

- **Settings → Advertio**: configure the API base URL and ingest key. The
  parent ingest switch also enables Housing; check Housing readiness first.
  Restart Telclaw after changing settings marked *restart required*.
- **Settings → Advertio Cargo**: set the dedicated cargo switch, backfill
  freshness window, batch size and daily request budget. Auto-publish is enabled
  by default for new unique cargo leads; disable it for untrusted sources.
- **Operations → Advertio Cargo — Website API**: separate from
  `@advertio_cargo`, shows AI processed/failed/pending, **ready to send now**,
  broad waiting/retry/rejected counts, sent/already-existed, expired/stale,
  today's requests and last recorded historical rejection.
  Recent rows include Telegram message ID, source, route, date, failure cause,
  and next retry time. This report is read-only and does not call the API.
- Click **Test Advertio API /health** first: it runs an unauthenticated HTTPS
  GET without sending a listing or the ingest key and reports reachability
  and HTTP status. A successful health response does not prove the ingest key
  is valid, but failures reveal DNS, TLS or API availability problems.
- Click **Send Cargo to Advertio website** with limit 10 for a small pilot.
  Results appear in the independent job status box. No housing media or
  Telegram message will be published by this action.
- **Requeue** is available only on individually rejected records. First fix
  invalid route/category/attribute data; blind retries of invalid data will
  fail again.
- **Stop Cargo Delivery** ends the current manual batch after any in-flight
  request returns.

Do not confuse a queued/failed Telegram publisher target with the website
API integration. They are separate destinations and have separate metrics.

## Operator flows

- Scheduled: `services/scheduler_service.py` runs the isolated cargo worker
  after the housing Advertio pass. It is protected by the same pipeline lock
  and obeys the Advertio stage-skip request.
- Terminal: the existing Advertio manual delivery menu also sends
  cargo candidates and reports separate stats.
- Backoffice: the existing Advertio delivery action also performs
  a cargo pass; the response contains `cargo` result stats.
- Rollback: `TELCLAW_ADVERTIO_CARGO_ENABLED=false` disables cargo only.
  Re-enabling resumes from durable statuses; Housing and Telegram continue.

Diagnostics: look for logger `telclaw.advertio.cargo`; `rejected` records
may require better mapping, a catalog update or source correction.
No 400 should be retried automatically. Inspect
`advertio_cargo_deliveries` before resetting a terminal rejection.

## Known external dependencies and limits

- API DNS/TLS plus backend ingest-key configuration must be verified in the
  target environment; source code alone cannot establish readiness.
- The platform currently catalogs cargo locations in **IR, CA, US, DE,
  IT, GB and FR**. Other routes cannot be published until its reference
  catalog is expanded.
- The existing Telegram publisher intentionally keeps its older required
  travel-date eligibility. This is not a reason to block undated cargo
  from the Advertio API.
- Automatic Telegram deletion-event reconciliation is not yet wired to
  `DELETE /api/ingest/leads/{sourceName}/{externalId}`. The destination's
  30-day TTL remains a fallback; future removal handling should use
  verified Telegram deletion events to avoid accidental data loss.

**Existing production configuration wins:** The `.env` or saved Back Office
`TELCLAW_ADVERTIO_CARGO_AUTO_PUBLISH=false` override remains authoritative;
changing repository defaults does not overwrite deployment settings. Inspect
Back Office → Settings → Advertio Cargo and switch it on explicitly if needed.
