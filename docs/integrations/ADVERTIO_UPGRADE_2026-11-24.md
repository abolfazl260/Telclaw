# Advertio integration upgrade — 2026-11-24

Status: **planned, not implemented**. The upstream [Crawler Integration Guide](CRAWLER-INTEGRATION.md) is preserved verbatim. The live category schema wins if it differs. This plan documents the reviewed Telclaw gaps and the full ordered backlog; each linked issue has detailed scope, acceptance criteria and test requirements.

## Contract delta and current implementation

Advertio expanded from 11 countries to 48 countries and 341 cities. Telclaw currently allows only 7 cargo countries (IR, CA, US, DE, IT, GB, FR), including queue and dashboard eligibility. The existing per-country cities request already uses limit=200; the cargo service has a process-local 24-hour per-country cache. A single unfiltered request cannot return all cities (200 rows, and the internal province lookup also caps at 200 of 211). The server now accepts cargo_type as a multi-select list, still accepting a legacy scalar. Telclaw already maps lists/JSON strings/delimited text to arrays, but the extraction prompt and full data lifecycle must explicitly support multiple types. Eight currency codes are supported by Advertio (CAD, USD, EUR, GBP, AED, CNY, TRY, RUB), unchanged in this release; Telclaw delivery currently accepts only the first four. No additional currency can be inferred from new countries. The five-word native-app minimum does not apply to ingest. Housing and Jobs have no new November 24 schema changes; the existing housing negotiable-price gap and shared request budget are separate follow-ups.

Already supported by current cargo delivery: optional travel date, omission of role, past-trip filtering, Gregorian/Jalali extraction instructions, empty mediaKeys, origin-based location, canonical route/date title, and array mapping. Keep these behaviors, existing internal passenger/shipper classification and Telegram publication. No rewrite is required.

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
