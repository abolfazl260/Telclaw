# Telclaw → Advertio Ingest Integration

## Purpose

Advertio is a separate delivery cohort for crawled Telegram housing listings. It must never be used for genuine user-created listings.

```text
Telegram
  ↓
Crawler
  ↓
Raw SQLite message
  ↓
Processing / dedup / cleaning
  ↓
Groq extraction
  ↓
housinglist + local validation
  ↓
AdvertioDeliveryService
  ├─ upload media
  └─ POST /api/ingest/leads
  ↓
Advertio
```

The integration is intentionally isolated under `delivery/` so the AI extractor does not know about Advertio HTTP details.

## Source cohort

The fixed default source is:

```text
telegram-rent
```

This value must remain stable. Advertio idempotency is `(sourceName, externalId)`.

Only crawled Telegram listings may enter this path. User-created listings must continue through their own application flow and must never be sent to Advertio Ingest.

## Configuration

The integration is disabled by default:

```env
TELCLAW_ADVERTIO_INGEST_ENABLED=false
TELCLAW_ADVERTIO_BASE_URL=https://api.advertio.ca
TELCLAW_ADVERTIO_INGEST_KEY=...
TELCLAW_ADVERTIO_SOURCE_NAME=telegram-rent
TELCLAW_ADVERTIO_AUTO_PUBLISH=false
TELCLAW_ADVERTIO_CONCURRENCY=3
TELCLAW_ADVERTIO_TIMEOUT_SECONDS=60
```

The real ingest key is never committed.

## Code ownership

### `delivery/advertio_client.py`
Transport-only HTTP client:

- `POST /api/ingest/media?source=...`
- `POST /api/ingest/leads`
- `DELETE /api/ingest/leads/{source}/{externalId}`
- `DELETE /api/ingest/sources/{source}`

It owns authentication, HTTP transport, status classification, and response parsing.

### `delivery/advertio_service.py`
Business mapping and Advertio validation:

- Maps `housinglist` to Advertio `housing`.
- Builds `attributesJson` as a **JSON string**.
- Enforces required `listing_type`, `property_type`, `bedrooms`, and CAD price.
- Requires `CA`, province, and city.
- Uses `message_id` as the stable `externalId`.
- Uses Telegram `message_link` and/or sender/contact handle for redirect.
- Uploads up to 10 local media files before lead creation.
- Keeps `autoPublish=false` by default.

### `ai/ai_service.py`
After a successful AI extraction and category persistence, it optionally invokes the delivery service only for `housinglist`. AI success is not converted into AI failure when Advertio rejects a listing; delivery has its own status.

## Housing contract required by Advertio

The AI housing schema contains these Advertio-relevant fields:

```text
property_type
listing_type
title
description
country_code
province
city
neighborhood
price
currency
bedrooms
bathrooms
area
furnished
rent_period
availability
contact
features
```

The AI must extract location components explicitly. A single free-form `location` is not enough for Advertio because country/province/city are required separately.

### Advertio required values

```text
listing_type: rent | roommate
property_type: apartment | condo | basement | studio | room | house
bedrooms: 0 | 1 | 2 | 3 | 4+
price: CAD 100–10,000/month
country_code: CA
province: valid Advertio catalog value
city: valid Advertio catalog value under the province
```

If these cannot be reliably extracted, **reject the listing**. Never guess price, bedrooms, property type, or location.

## Field mapping

| Telclaw | Advertio |
|---|---|
| `message_id` | `externalId` |
| `message_link` | `sourceUrl` |
| `sender_username` / `contact` | `contactHandle` |
| `title` | `title` |
| `description` | `description` |
| housing category | `categorySlug: housing` |
| housing attributes | JSON-string `attributesJson` |
| `country_code` | `countryCode` |
| `province` | `province` |
| `city` | `city` |
| `neighborhood` | `neighborhood` |
| uploaded media key | `mediaKeys[]` |
| config | `autoPublish` |

## `attributesJson`

Advertio expects a string, not a JSON object:

```json
{
  "attributesJson": "{\"listing_type\":\"rent\",\"property_type\":\"apartment\",\"bedrooms\":\"2\",\"price\":2100}"
}
```

Unknown optional attributes are omitted. Unknown required attributes cause the listing to be rejected locally before the API call.

## Currency

Telclaw's canonical platform currency is CAD. The AI layer already normalizes non-CAD currency to null rather than inventing an exchange rate. Advertio delivery then rejects a housing listing unless its price is a valid CAD value.

There is deliberately no currency conversion in the crawler.

## Media

Advertio requires media upload before lead creation:

```text
POST /api/ingest/media?source=telegram-rent
```

The returned `key` is sent in `mediaKeys`. `url` and `thumbUrl` are not persisted by Telclaw.

Advertio limits each file to 8 MB and each lead to 10 media keys. Telclaw currently has a single `media_path` per crawled message, so the adapter uploads the available local path and is ready for a future multi-media representation.

## Idempotency

Never hash the text to create `externalId`.

Use:

```text
externalId = Telegram message_id
```

Advertio returns `200` with `alreadyExisted=true` on a duplicate. Telclaw treats that as success (`advertio_status=already_existed`) and never retries it.

## Error policy

```text
2xx                  → success
200 alreadyExisted   → success, no retry
400                  → permanent rejection, no retry
401                  → configuration/auth error
5xx                  → retryable
network timeout      → retryable
```

The integration stores delivery state independently of AI state:

```text
waiting
  ↓
sent / already_existed
  ↓
rejected       (permanent 4xx)
retry          (5xx/network)
```

## Deletion

When the original Telegram post is known to have gone away, call:

```text
DELETE /api/ingest/leads/{sourceName}/{externalId}
```

A `204` or `404` is success. This operation is deliberately not triggered merely because an AI extraction fails. It must correspond to the source post being gone.

The whole source can be deactivated with:

```text
DELETE /api/ingest/sources/{sourceName}
```

Use that only when the channel/source should be disabled globally.

## Publishing policy

Start with:

```env
TELCLAW_ADVERTIO_AUTO_PUBLISH=false
```

This creates `PendingReview` listings. Only switch to `true` after a source has been manually reviewed and extraction quality is stable.

## Performance

Advertio documents ~5,000 listings/month and recommends concurrency 2–3 despite no API rate limit because media processing shares infrastructure with the database/storage.

Telclaw therefore does not implement an unbounded upload fan-out. `TELCLAW_ADVERTIO_CONCURRENCY` is capped at 3.

## Future work

1. Add a dedicated Advertio delivery queue/worker for retryable 5xx/network failures.
2. Add Telegram deletion/update event handling so the Advertio DELETE operation is triggered from a verified source deletion event.
3. Extend crawler media persistence from one `media_path` to an ordered media collection so all available photos can be uploaded (maximum 10).
4. Add an optional connection/test command that verifies `X-Ingest-Key` without creating a lead.
5. Keep Advertio-specific fields in the housing schema; do not hide required country/province/city data only inside the delivery adapter.
