# Advertio integration upgrade — 2026-11-24

The [upstream Crawler Integration Guide](CRAWLER-INTEGRATION.md) is preserved verbatim. The implementation for issues #196–#200 is prepared for staged testing. Issue #201 still requires Advertio backend readiness confirmation.

## Rollout and rollback

1. Back up SQLite through the normal backup procedure, then install and restart Telclaw on the configured API host. The migration adds tables and nullable or defaulted fields. Previously sent records retain their delivery states and source IDs.
2. Operator-approved Cargo policy: `TELCLAW_ADVERTIO_CARGO_AUTO_PUBLISH=true` requests publication for all valid Cargo submissions, including backfill, regardless of reviewed-source lists. Advertio retains validation and possible-duplicate moderation. This is a deliberate local policy departure from the upstream initial-review guidance. Housing and Jobs still require approved sources; use `TELCLAW_ADVERTIO_JOBS_REVIEWED_SOURCES` and `TELCLAW_ADVERTIO_HOUSING_REVIEWED_CHANNELS` respectively. The lists are comma-separated and loaded at restart.
3. Run a small delivery batch to warm the host-specific cargo snapshot. Active Cargo and Operations show coverage, timestamps, refresh failures and the shared budget from SQLite. The catalog refreshes after 24 hours. Malformed, capped and regressed results retain the previous snapshot and retry after at least 15 minutes. A missing snapshot defers cargo. A stale snapshot can send resolved routes while unresolved routes wait for refresh.
4. Valid, unsent waiting/retry cargo records from before deployment enter the normal bounded delivery queue automatically, regardless of country. The live route and date validation still applies. Historical Cargo submissions request publication when the Cargo switch is enabled. For previously rejected records, run a bounded historical dry run and apply only reviewed local row IDs; use `--after` to page.

   ~~~sh
   python -m scripts.advertio_review cargo-history --limit 25
   python -m scripts.advertio_review cargo-history --limit 25 --apply 123 456
   python -m scripts.advertio_review cargo-routes --limit 100
   python -m scripts.advertio_review duplicates --limit 100
   ~~~

5. Review accepted statuses, route counts and duplicate audits. The returned lead ID and status are audit evidence. Back Office shows the last recorded ingest status, not live publication state; existing PendingReview records must be approved in Advertio, not resubmitted. Deletion uses the originally submitted source key. The receiver can keep possible duplicates in `PendingReview`.
6. For rollback, pause Advertio delivery before reverting the deployment. Keep the host, SQLite database and successful delivery states. An older release will leave the new deletion queue pending.

The shared budget defaults to 1,000 HTTP attempts per UTC day, including media, schema and location reads, lead writes, deletion and the manual health probe. Admission paces starts to about 6.7 requests per second and caps concurrent HTTP across processes at `TELCLAW_ADVERTIO_CONCURRENCY` (1–3). The daily budget protects a 5% share each for Housing, Cargo, Jobs, catalog and deletions, with the remaining capacity shared. HTTP 429 and ingest authentication or configuration faults pause shared traffic. Confirm the actual quota window with Advertio before adjusting limits.

After a successful crawl, an explicit Telegram ID lookup checks up to 20 tracked posts alongside a live anchor from the same channel. Two complete checks at least six hours apart must return an explicit empty record for the same ID before a deletion is queued. Private channels, access errors and missing pages do not count. HTTP 204 and a listing-specific 404 finish the deletion. An ambiguous deletion 404 is retried for that listing. It can also mean the ad is already absent, so it leaves other ingest requests running. Historical deliveries without a recorded original API destination and verified source identity require reconciliation.

The tables below capture the original issue scope and acceptance criteria.

## P0 — Required

| # | Change | Detailed scope | Tracking |
|---|---|---|---|
| 1 | Dynamic route countries | Fetch live allowed origin/destination options from GET /api/categories/passenger-cargo/attributes and reference countries from GET /api/locations/countries. Remove fixed 7-country restriction; support all valid current and future countries without deployment. | [#196](https://github.com/abolfazl260/Telclaw/issues/196) |
| 2 | Synchronize city catalog | Fetch each country with GET /api/locations/cities?countryCode=XX&limit=200; refresh before next run when missing/stale, persist a validated atomic local snapshot, default 24h TTL, survive restarts and incomplete refresh, no per-crawl/per-listing network requests. Defer when reliable catalog unavailable. | #196 |
| 3 | Country-aware normalization | Common origin/destination resolver using code, English/Persian names and curated aliases; distinguish same-name cities by country, reject ambiguous/unlisted cities, never substitute a nearby city; use origin catalog provinceCode and city code. | #196 |
| 4 | Queue eligibility | Replace fixed country filters in delivery/advertio_cargo.py, manual/scheduled sends and Active Cargo backoffice with common snapshot rules; preserve ordering, freshness, retry and terminal states, no API on UI reads. | #196 |
| 5 | Shared request budget | One persistent limiter across Cargo, Housing and Jobs; count actual lead POSTs, media uploads, reference GETs, DELETEs and retry attempts. Approximately 1,000/day shared (confirm window with team), nginx 10 req/s burst 20, 2–3 aggregate concurrency or serial. Queue overflow, honor Retry-After and shared cooldown, bounded backoff, no retries for unchanged 400 payloads, no new workers/tight polling. Existing journey caps may remain sublimits. | [#197](https://github.com/abolfazl260/Telclaw/issues/197) |

## P1 — Extraction and delivery

| # | Change | Detailed scope | Tracking |
|---|---|---|---|
| 6 | Multi-select cargo_type | Extract every explicitly stated type (documents, clothes, electronics, food, other); dedupe; send JSON array within attributesJson string. Audit prompt, schema, storage/serialization, legacy scalar compatibility, normalization, reporting, exports and Telegram display. Omit unknown rather than invent. | [#198](https://github.com/abolfazl260/Telclaw/issues/198) |
| 7 | Currency completion | Support AED, CNY, TRY, RUB in addition to CAD, USD, EUR, GBP; multilingual source-text evidence; do not guess currency from route or convert. | #198 |
| 8 | Cargo price validation | Only send numeric price explicitly per kg and with one of eight stated currencies. Omit price/currency for unsupported currencies (toman/rial, AUD, AZN, etc.), flat/total fee or ambiguous currency/unit; retain in original description. Explicit negotiable flag only, mutually exclusive with price; weight_kg 0.1–30. | #198 |
| 9 | Safe historical recovery | Dry-run and revalidate previously processed, unsent country/city-only exclusions/rejections under new catalog. Bounded requeue only valid fresh/not-expired records; exclude successful, deactivated, unrelated failures; preserve stable IDs, audit and moderation history. No automatic AI reprocessing/recrawl or bulk republish. | #198 |
| 10 | Negotiable housing price | Coordinate with [#182](https://github.com/abolfazl260/Telclaw/issues/182). Only explicit negotiable rent may send price_negotiable=true with no price; never both, never use as fallback for missing extraction, keep required fields and CAD validation for numeric price. Audit schema, storage, validation, warnings and regression tests. No Jobs negotiable flag. | [#199](https://github.com/abolfazl260/Telclaw/issues/199) |
| 11 | Preserve source descriptions | Cargo and Jobs use original raw_text, compatible original-text fallback if absent, preserve language, price and content, max 2,000 chars, no translation/AI rewriting or five-word ingest gate. Retain canonical titles. | [#200](https://github.com/abolfazl260/Telclaw/issues/200) |

## P2 — Reliability and monitoring

| # | Change | Detailed scope | Tracking |
|---|---|---|---|
| 12 | First-send duplicate audit | alreadyExisted=true is success, not retry. Persist/log first-send sourceName, externalId, local ID and returned leadId; distinguish known retry vs possible cross-source duplicate, report to backend. Respect PendingReview, never delete another source's lead or mutate existing by resending. | #200 |
| 13 | Source deletion lifecycle | Contract-required DELETE when original post confirmed removed, using original stable source/external ID; durable bounded queue, 204/confirmed missing lead 404 success, no false deletion on incomplete crawl/access failures, source-wide deletion explicit operator only. Never reactivate by re-crawl; TTL/travel-date hiding not deletion. | #200 |
| 14 | Catalog status | Existing backoffice shows country/city counts, per-country coverage if useful, last successful refresh, next eligible refresh and stale/error state; read local state only. | #200 |
| 15 | Unsupported-city report | Aggregate missing/ambiguous country/city by direction, country, raw name, count, sample message ID/link and last seen; deduplicated warnings and actionable reports to Advertio. | #200 |
| 16 | Documentation | Retain entire upstream guide, update implementation docs/config/runbook/README as each PR ships, explicitly distinguish planned and deployed behavior. | #200 |
| 17 | Regression tests | Current/legacy data, schema arrays, normalization/catalog completeness/restart/failure, price/currency, source descriptions, duplicate/delete lifecycle, queue/cooldown/budget, manual/scheduled parity, dashboard no-network and existing Telegram behavior. Run full suite before merging implementation. | #200 |

## P3 — Blocked by backend

| # | Change | Detailed scope | Tracking |
|---|---|---|---|
| 18 | Host migration | Current default https://api.advertio.net; new contract names https://api.advertio.ir but explicitly warns it is not live. Keep TELCLAW_ADVERTIO_BASE_URL configurable; migrate only after backend confirms DNS, TLS, GET /health and Cloudflare rule. Verify authorized test ingestion and schema, shared budget, stable identity continuity, cache namespace and rollback. No unapproved production test, no automatic cross-host fallback. | [#201](https://github.com/abolfazl260/Telclaw/issues/201) |

## Implementation order and safeguards

Implement #196 and #197 first (coordinate the global limiter before activating expanded catalog/backfill); then #198 and #199, and #200 in reviewable changes. #201 remains blocked. Each PR should include focused tests and implementation docs. Use existing scheduler/workers, persistent caches, bounded batches, low concurrency and queue deferral; no redundant AI calls, recurring tight polling or unnecessary media uploads. No changes to existing Telegram delivery semantics. Preserve existing sourceName/externalId and successful states; do not change identity to force updates. New sources and changed extraction start with autoPublish=false until manually reviewed. Keep Housing/Jobs Canada-only and mediaKeys=[] for Cargo/Jobs. Audit poster handle/positive real user ID provenance, no channel or admin proxy, sourceUrl fallback, crawled cohort isolation, field limits and removed Jobs fields as specified in #200 and the complete upstream contract.

This documentation commit registers the contract and plan only; it does not implement, enable, or deploy runtime changes.
