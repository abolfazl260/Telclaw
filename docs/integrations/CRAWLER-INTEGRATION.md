# Advertio Ingest API — Crawler Integration Guide

**Audience:** whoever is writing the crawler.
**Server-side status:** ✅ built and tested (ADR-0009). This document is a contract, not a proposal.
**Journeys that accept listings:** `housing` · `passenger-cargo` · `jobs`
**Last updated:** 2026-11-24

> **What changed on 2026-11-24 (cargo routes and item type).** The location catalog grew, and
> `cargo_type` is a list. Refresh the catalog before the next run.
>
> - **48 countries, 341 cities.** `origin_country` and `destination_country` accept 37 countries
>   they didn't before (§7.2), and cities were added inside countries you already send — Iran,
>   Germany, China, the United States, Italy, the United Kingdom and Russia among them. A route
>   the server used to reject for an unknown country or city can be a real listing now, once the
>   city is on the list.
> - **One request no longer returns the whole catalog.** The page size is still capped at 200,
>   and 341 cities do not fit. Fetch cities **once per country**, with `limit=200` (§8). A lookup
>   built from the old eleven-country dump will 400 cities that are valid, and a single
>   unfiltered call drops cities past the cap.
> - **`currency` did not grow with the countries.** It is still `CAD` · `USD` · `EUR` · `GBP` ·
>   `AED` · `CNY` · `TRY` · `RUB`. A price in any other currency (an Australian dollar, a dram, a
>   toman) still has no field. Leave `price` out (§7.2).
> - **`cargo_type` is a list.** Send every kind the post names, as an array. A single string is
>   still accepted and stored as a list of one, so a crawler that already sends one value does
>   not break (§7.2).
> - **The five-word description rule does not apply on this path.** A listing a person posts in
>   the app now needs at least five words. Ingest does not check that. Keep sending the post text
>   for cargo and jobs (§4), including a short one.
>
> **What changed on 2026-10-21 (cargo only).** Cargo is one pool for now: the app no longer asks
> whether someone is travelling or sending, and the date is optional.
>
> - **`role` and `travel_date` are no longer required** for `passenger-cargo` (§7.2). Leave
>   `role` out. The server stores every cargo listing that doesn't name a side as `traveler`.
> - **A post with no specific date is no longer rejected.** Send it without `travel_date`.
>   Never guess a date.
> - **`price` is per kg** on every listing you send without a `role`.
> - The title is `{origin} → {destination} · {d MMM}`, or just `{origin} → {destination}` when
>   there is no date. It no longer starts with "Travelling" or "Sending".
> - Nothing you already send breaks. A `role` you still send is validated and kept.
>
> **What changed since 2026-08-31.** Two new journeys went live on 2026-10-19, and both take
> crawled listings through the same endpoints:
>
> - **Passenger cargo** (`passenger-cargo`): a trip between two cities. See §7.2.
> - **Jobs** (`jobs`): a job opening, or a person looking for work, in Canada. See §7.3.
> - **Housing hasn't changed.** Nothing you already send needs to change.
> - The note saying jobs uses `salary_negotiable` is out of date and has been removed. Jobs
>   now has no "negotiable" flag (§7.3).
> - **New in §4:** what it means when `alreadyExisted: true` comes back for a *different* post.
>   Cargo and jobs posts look alike much more often than housing posts do.
> - **New in §8:** the location catalog covered eleven countries, and only cargo routes used
>   them. It has grown since; the current list is in the 2026-11-24 note and in §7.2.
>
> No backend change was needed. Ingest has always checked a listing against the schema of the
> category it names. However, your first crawled cargo and jobs runs are the first real traffic
> of that kind. Start them with `autoPublish: false` (§3).

## Base URLs

Throughout this document, `{base_url}` means the API's own domain — today that's
**`api.advertio.ir`**. Keep it as a **config variable** in the crawler and never hardcode it;
it may change.

| | URL | |
|---|---|---|
| API | `https://{base_url}/api/…` | every endpoint in this document |
| Media | `https://media.advertio.ir/advertio-media/…` | read-only. The crawler never reads from here |

> `api.advertio.ir` is a separate hostname from the one the web app runs on, fronting the
> same backend. It exists so external traffic (this crawler, and any future partner
> integration) sits behind its own edge rate-limit bucket instead of sharing one with real
> users — see ADR-0012. The path shape is unchanged: still `/api/…`, only the host moved.

> ⚠️ **`api.advertio.ir` is not live yet.** DNS, the TLS certificate, and the Cloudflare
> rate-limit rule are still being rolled out (ADR-0012, Migration path). Confirm with the
> backend team that it resolves and answers `GET /health` before pointing a real crawler run
> at it — until then it will fail closed, not silently degrade.

---

## 0. Before anything else — this cohort is separate and must stay separate

Any listing created through this path gets `supplySource = Crawled`. This is not a cosmetic
label — three behaviors are tied to it, and they are the same in every journey:

| | User-submitted listing | Listing from this path |
|---|---|---|
| Contact | coins are deducted (each journey sets its own price) | **coins are never deducted** — the user is redirected to Telegram |
| Gate A metric | counted, in its own journey's Gate A | **fully excluded** from every journey's Gate A |
| Display | phone / identity verification badges | **no verification badge**; the contact button reads "Contact via Telegram" |

The reasoning is in `.specify/memory/constitution.md` §IV and is NON-NEGOTIABLE. For you, this
means: **never send a real user's listing through this path.** If someone signed up with us
directly, their path is `POST /api/leads`, not this one.

---

## 1. Authentication

Send the `X-Ingest-Key` header on **every** request:

```
X-Ingest-Key: <key>
```

| | |
|---|---|
| Where to get the key | from the backend team. On the server it's `INGEST_API_KEY` |
| Minimum length | 32 characters (`openssl rand -hex 32`) |
| Wrong key or missing header | `401` |
| **Getting a `404`** | means the key isn't configured on the server, so the whole route group isn't mapped. That's on us, not you — tell the team |

The app itself does not rate-limit this route (deliberately: the 20-requests-per-minute cap on
other routes would strangle a crawler run). Edge nginx does apply a general cap, though —
**10 requests/second with a burst of 20**, per IP, on `api.advertio.ir` specifically (its own
zone, separate from the app's own traffic — ADR-0012). Following §6 keeps you well under that; a
`429` means you're sending too much in parallel.

That nginx number is a flood absorber, not the real budget. The actual daily ceiling (§6) is
enforced as a Cloudflare Rate Limiting Rule on `api.advertio.ir`, which returns a `429` the same
way — same handling applies.

---

## 2. Ingestion flow — three steps, in this order

This section is deliberately numbered because it really is a sequence.

### Step 1 — upload photos (housing only, if the listing has any)

> **Skip this step for cargo and jobs.** Their listings have no photo slot, on the card or on
> the detail page. Send `mediaKeys: []`. An upload makes the production server decode and
> re-encode the image, and for these listings nobody would ever see it.

```http
POST /api/ingest/media?source=telegram-rent
Content-Type: multipart/form-data
X-Ingest-Key: <key>

form field: file
```

`200` response:

```json
{
  "key": "leads/crawler-telegram-rent/9f2c1ab34d5e6f708192a3b4c5d6e7f8",
  "url": "https://media.advertio.ir/advertio-media/leads/crawler-telegram-rent/9f2c…/thumb.webp",
  "thumbUrl": "https://media.advertio.ir/advertio-media/leads/crawler-telegram-rent/9f2c…/thumb.webp"
}
```

Keep `key` — that's what you send back in step 2. `url` and `thumbUrl` are only for preview and
**must not be persisted**.

> **Why `url` and `thumbUrl` are identical:** only the 480px variant is stored for this cohort.
> The 1600px variant is never generated, because the user is redirected to Telegram for a
> crawled listing and never actually views it in the app — and 5,000 listings/month with both
> variants would add up to ~30 GB/year on the same disk the database writes to. This is
> intentional, not a bug.

Constraints:

| | |
|---|---|
| Format | JPEG · PNG · WebP (detected from file bytes, not from your `Content-Type`) |
| File size | 8 MB max |
| Dimensions | 50 megapixels max |
| `source` | must match the `sourceName` you send in step 2, or the key is rejected in step 2 |
| Metadata | EXIF/GPS is stripped server-side — no action needed on your end |

### Step 2 — create the listing

```http
POST /api/ingest/leads
Content-Type: application/json
X-Ingest-Key: <key>
```

```jsonc
{
  "sourceName": "telegram-rent",
  "externalId": "4021",
  "sourceUrl": "https://t.me/some_channel/4021",
  "contactHandle": "@landlord",
  "posterTelegramUserId": 123456789,

  "title": "Bright 2-bed near Yonge & Eglinton",
  "description": "Cleaned-up post text",

  "categorySlug": "housing",
  "attributesJson": "{\"listing_type\":\"rent\",\"property_type\":\"apartment\",\"bedrooms\":\"2\",\"price\":2100}",

  "countryCode": "CA",
  "province": "Ontario",
  "city": "Toronto",
  "neighborhood": "Midtown",

  "mediaKeys": ["leads/crawler-telegram-rent/9f2c1ab34d5e6f708192a3b4c5d6e7f8"],
  "autoPublish": false
}
```

This example is a housing listing. Cargo and jobs use the same request with their own
`categorySlug` and `attributesJson`. See §7.2, §7.3 and the examples at the end.

Responses:

| Status | Body | Meaning |
|---|---|---|
| `201` | `{"leadId":"…","status":"PendingReview","alreadyExisted":false}` | created |
| `201` | `{"leadId":"…","status":"Active","alreadyExisted":false}` | created and published (`autoPublish: true`) |
| `201` | `{"leadId":"…","status":"PendingReview","alreadyExisted":false}` | created, but flagged as a possible duplicate of an existing listing — **stays PendingReview for human review even if you sent `autoPublish: true`** |
| `200` | `{"leadId":"…","status":"…","alreadyExisted":true}` | **already existed: either this same post, or one the server judged to be the same ad (§4). Nothing was changed.** Not an error |
| `400` | a JSON string with the error message | see §5 |
| `401` | — | bad or missing key |

### Step 3 — remove the listing once the source post is gone

**This step is not optional.** A crawled listing has no publisher to mark it as rented, filled
or no longer available, and we never update an existing listing in place. If you don't remove
it, it stays in the feed for up to 30 days (TTL). A rented flat, a filled job or a trip that has
already left: a dead listing is the worst thing a user can see.

Cargo has one extra wrinkle. The feed hides a trip once its travel date has passed, but the
listing itself stays live until its 30 days are up. A trip with no date is never hidden this
way. Delete it when the post is removed, the same as for housing.

```http
DELETE /api/ingest/leads/telegram-rent/4021      → 204   (or 404 if not found)
DELETE /api/ingest/sources/telegram-rent          → 200 {"deactivated": 137}
```

The second call deactivates an entire source at once — use it when a channel turns out to be
spam, or the crawler was broken for a while. Use with care.

---

## 3. Field reference

| Field | Required | Rule |
|---|---|---|
| `sourceName` | ✅ | `^[a-z0-9][a-z0-9-]{1,31}$` — lowercase letters, digits and dashes only, 2–32 characters. **Must stay constant**; changing it orphans every previous listing from that source. Don't reuse a housing source's name for a cargo or jobs channel: a source-wide `DELETE` (§2) would remove both channels' listings together |
| `externalId` | ✅ | the post's id at the source (e.g. the Telegram message id). Max 200 characters. **The idempotency key** — see §4 |
| `sourceUrl` | ⚠️ | link to the original post. Max 500 characters |
| `contactHandle` | ⚠️ | the **poster's** Telegram handle (not the channel's), with or without `@`. Max 64 characters. Also the ownership-claim key — see below |
| `posterTelegramUserId` | — | the **poster's** Telegram numeric user id (`from.id`), as a JSON number. Only when the post really exposes its author — see below. Positive; never a chat, channel or message id |
| `title` | ✅ | max 200 characters. For cargo and jobs, build it from the attributes the same way the app does (§7.2, §7.3) |
| `description` | — | max 2,000 characters. **For cargo and jobs, always send the post text.** See §4. The five-word minimum on listings a person posts in the app does not apply here |
| `categorySlug` | ✅ | `housing` · `passenger-cargo` · `jobs`. `GET /api/categories` also lists `services` and `social`. Those have no journey in the app yet, so a listing sent there is stored but never shown to users. Don't send them |
| `attributesJson` | ✅ | **a JSON string, not an object** — see §7 |
| `countryCode` | ✅ | `CA` for housing and jobs. For cargo, the country of the trip's origin (§7.2) |
| `province` | ✅ | name or code (`Ontario` or `ON`). For cargo, the origin city's province |
| `city` | ✅ | name or code (`Toronto` or `toronto`). For cargo, the origin city |
| `neighborhood` | — | free text, max 100 characters. Not validated |
| `mediaKeys` | — | housing only (§2 Step 1). Up to 10 keys from step 1. **The first key is the feed card's cover photo** |
| `autoPublish` | — | defaults to `false` |

⚠️ = **one of `sourceUrl` or `contactHandle` is required.** Without either, the "Open in
Telegram" button has nowhere to go and the listing is a dead end. The server rejects it.

The redirect target is chosen in this order: `t.me/{contactHandle}` if present, otherwise
`sourceUrl`.

### `contactHandle` does two jobs — it is your most valuable optional field

Besides being the redirect target, it is the **ownership-claim key**. When the person who
posted the original ad later signs in to the platform with Telegram, every crawled listing
whose `contactHandle` matches their Telegram username is **transferred to their account
automatically** and shows up under their own listings.

That transfer is the only path by which a crawled listing becomes a listing from a *real*
publisher. Which means:

> **Every post whose handle you fail to extract is a user you will never acquire.** If you are
> weighing "just send it without the handle" against "spend more effort on handle extraction",
> pick the second.

Details:
- Send it with or without `@`; the server lowercases and strips it. No need to normalise.
- **Send the poster's handle, not the channel's.** If you send the channel handle, the first
  person who signs in with it inherits *every* listing from that channel.
- If you are not confident the handle belongs to the poster, leave it empty and send only
  `sourceUrl`. A wrong transfer is worse than no transfer.
- Transfer happens once. A listing that has already been claimed is never handed to a second
  person, even if the handle later changes owner on Telegram.
- The listing keeps `supplySource: "Crawled"` after the transfer. Only the publisher's *new*
  listings count as user-supplied.

> `title`, `description`, `sourceUrl`, `contactHandle` and `neighborhood` are only enforced as
> database column limits, not as an application-level check — a value over the limit is not
> guaranteed to come back as a clean `400` with a friendly message. Truncate or validate these
> client-side before sending.

### `posterTelegramUserId` — only when Telegram gave it to you

The stable identity behind the handle: a username can change hands, the numeric user id can't.
Admins see it on the listing's review drawer; it never appears in any public API.

- Send it only when the update carries an **individual author** — the message's `from.id` in a
  group. A channel post has no individual author (its `sender_chat` is the channel): **omit the
  field** there. Never send the channel's or chat's id instead.
- Telegram user ids are positive. Chat and channel ids are negative (`-100…`), so a zero or
  negative value is refused with a `400` rather than stored.
- It can exceed 32 bits — keep it in a 64-bit integer end to end.
- Never look it up from a username, and never guess it. Missing is fine; wrong is not.
- It is recorded with the listing's first ingest. A re-send of the same `externalId` doesn't
  change it.

### `autoPublish` — which one should I send?

| | `false` (default) | `true` |
|---|---|---|
| Status | `PendingReview` — awaiting admin approval | `Active` — immediately in the feed |
| When to use it | **any new source, new crawler, new journey, or any change to your extraction logic** | only a channel that has already been manually reviewed and proven reliable **for that journey** |

Start with `false`. Flipping it later is a one-line change.

A channel you already trust for housing is not automatically trusted for cargo or jobs. Each
journey has its own parser, and a new parser starts at `false`.

`true` is still only ever a *request*: if the post is flagged as a possible duplicate of an
existing listing (score high enough to warrant a human look, not high enough to reject outright),
it is created at `PendingReview` regardless — a trusted feed can auto-publish a new-looking post,
never a possible duplicate.

---

## 4. Idempotency — the most important section in this document

The pair `(sourceName, externalId)` is unique. **Re-running the crawler over the same channel
must be a no-op, and it is.**

- On the second run you get `200` with `alreadyExisted: true`. This is **success**, not an
  error — do not log it as an error and do not retry.
- The existing listing is **not updated** — even if the title or price changed in the original
  post. This is intentional: an approved or rejected listing carries an admin's decision on it,
  and an automatic overwrite would erase that decision.
- If you already deactivated a listing via step 3, re-crawling it **does not bring it back**.
  The key is the same.

So build `externalId` to be stable. A Telegram message id is a good choice. A hash of the post
text is a **bad** one — a small edit to the post creates a duplicate listing.

### When `alreadyExisted: true` is about a different post

Before creating anything, the server also checks whether it already has the same ad from any
source. If any of these checks matches, it returns `200` with `alreadyExisted: true` and the
**existing** listing's `leadId`:

| Check | Matches when |
|---|---|
| Link | the same normalized `sourceUrl`, in the same category |
| Content | the same title, description, price and city, in the same category |
| Similarity | a weighted score of 0.92 or more against listings in the same category and city from the last 60 days. The weights are title 35%, description 35%, price 20% and photos 10% |

For cargo, "city" means the trip's origin. A score between 0.75 and 0.92 still creates the
listing, but holds it at `PendingReview` (§3, `autoPublish`). The thresholds and weights are
configuration values and can change.

Nothing is stored under your `externalId` in that case. Re-sending it gets the same answer, and
a `DELETE` for it returns `404`.

On housing this is rare and almost always right: it's the same flat posted in two channels. On
cargo and jobs it's the first thing to look at, because those posts are short and look alike:

- **Always send the post text as `description`.** Ingest does not apply the five-word minimum
  that listings posted in the app now have, so a short post is still valid here — send it as
  written. A cargo or jobs title is built from the
  attributes (§7.2, §7.3), so two posts on the same route and date get the same title (and a
  cargo post with no date has only the route).
  If they also quote the same price and you send no description, the second post is refused as
  a copy of the first. A different person's trip is dropped without any error.
- Even with the text included, a channel that posts every trip from one fixed template, where
  only the handle changes, can score two different people's posts above 0.92. By default the
  contact handle carries no weight in the score.
- **Log every `alreadyExisted: true` that comes back on the first send of an `externalId`**,
  with both your id and the returned `leadId`, and report them to the team. That's how we'll
  find out whether these journeys need different thresholds.

---

## 5. Errors

The `400` response body is a JSON string, e.g. `"Unknown category 'huosing'."`

**Request-level validation** (checked before `attributesJson` is even parsed):

| Message | Cause |
|---|---|
| `sourceName must be 2–32 characters…` | `sourceName` doesn't match the pattern |
| `externalId is required…` | you sent an empty value |
| `A crawled listing needs somewhere to send the buyer…` | neither `contactHandle` nor `sourceUrl` was sent |
| `posterTelegramUserId must be the author's Telegram user id…` | zero or negative — a chat or channel id was sent in its place |
| `Unknown category '…'` | invalid `categorySlug` |
| `That location isn't available…` | city/province not in the catalog — see §8 |
| `A listing can have at most 10 photos.` | more than 10 keys in `mediaKeys` |
| `One or more photos were not uploaded by this source.` | a key belongs to a different `source`, or was tampered with |

**`attributesJson` validation** (see §7 for the attribute schema):

| Message | Cause |
|---|---|
| `Attributes must be a JSON object.` | `attributesJson` isn't valid JSON, or isn't a JSON object |
| `'{key}' is not a valid field for this category.` | unknown attribute key — including a key that was removed (§7.3) |
| `{Label} is required.` | a required attribute is missing — for housing's `price`, unless `price_negotiable` is `true` |
| `{Label} must be a number.` / `must be at least {min}.` / `must be at most {max}.` | a `number` attribute is invalid or out of range |
| `'{value}' is not one of the allowed values for {Label}.` | a `select` or `multi-select` value isn't in the enum — **see the value lists in §7** |
| `{Label} must be a list.` | a `multi-select` attribute wasn't sent as an array |
| `{Label} must be yes or no.` | a `boolean` attribute isn't `true`/`false` |
| `{Label} must be a date.` | a `date` attribute isn't a valid `YYYY-MM-DD` |
| `{Label} must be a range with a start and an end.` / `must be a range of numbers.` / `starts after it ends.` / `must start at {min} or above.` / `must end at {max} or below.` | a `range` attribute (e.g. `age_range`) is malformed or out of bounds |
| `{Label} must be {500} characters or fewer.` | a `text` attribute is too long |
| `{Label} can't have a value when {Flag label} is on.` | you sent a `price` **and** `price_negotiable: true` — see §7.1 |
| `'{value}' is not a city on the list for {Label}.` | a cargo `origin` or `destination` isn't a catalog city of its country — see §7.2 |

`{Label}` is the attribute's display label (e.g. "Monthly Rent (CAD)"), not its key.

**All `400`s are permanent — do not retry.** Reject that listing, log it, and report it to the
team. Only `5xx` is worth retrying.

---

## 6. Volume and concurrency

| | |
|---|---|
| Expected volume | ~1,000 requests/day sustained — revised up from the original ~20,000/month estimate; this is what moved the API to its own domain (ADR-0012) |
| App rate limit | none |
| Edge rate limit (nginx, `api.advertio.ir`) | 10 req/s with a burst of 20, per IP — a flood absorber, not the budget |
| Daily budget (Cloudflare Rate Limiting Rule) | ~1,000 req/day on `api.advertio.ir`, **shared by all journeys** |
| **Recommended concurrency** | **2–3** |

The daily budget is one budget for every journey together. Adding cargo and jobs channels
doesn't raise it. If all three together will need more than ~1,000 requests a day, tell the
team before you go over, not after.

Why limit it if there's no hard cap? Every photo upload does a decode and an encode on the same
server that hosts the database and storage. 500 parallel uploads will slow the app down for real
users. Serial, or a concurrency of 3, is more than enough — at this volume you won't come close
to any ceiling anyway.

Listing lifetime: `Early` for 72 hours, then `Public`, expiring 30 days after publication. All
three numbers are read from the database and can change — don't hardcode assumptions about them.
Each journey has its own copy of these numbers. Cargo and jobs started with housing's values.
A cargo trip does not expire on its travel date: from that date on the feed hides it, but it
stays live until its 30 days are up (§2, Step 3).

---

## 7. `attributesJson` — it's a string, not an object

The single most likely mistake. This field is a **string** that contains JSON:

```jsonc
// ✅ correct
"attributesJson": "{\"listing_type\":\"rent\",\"price\":2100}"

// ❌ wrong
"attributesJson": { "listing_type": "rent", "price": 2100 }
```

### Attribute types

| Type | Shape |
|---|---|
| `select` | one string from a fixed enum |
| `multi-select` | an array of strings, each from a fixed enum |
| `number` | a JSON number (a numeric string is also accepted, but send a number) |
| `boolean` | `true` / `false` |
| `date` | `YYYY-MM-DD` |
| `range` | `[from, to]`, both numbers from a fixed range |
| `text` | free text, max 500 characters |

Each journey has its own attribute set: housing in §7.1, cargo in §7.2, jobs in §7.3. If you
can't extract a **required** attribute from a post, **reject that listing**. Don't guess. A
guessed value puts bad data in the feed and breaks the filters for everyone.

### 7.1 Housing — `housing`

#### Required attributes

| Key | Type | Allowed values |
|---|---|---|
| `listing_type` | select | `rent` · `roommate` |
| `property_type` | select | `apartment` · `condo` · `basement` · `studio` · `room` · `house` |
| `bedrooms` | select | `0` · `1` · `2` · `3` · `4+` (string, not a number) |
| `price` | number | monthly rent in CAD · 100–10,000 · **required unless `price_negotiable` is `true`** |

The only exception to "reject when a required attribute is missing" is a post that says its
price is negotiable, described below.

#### When the post says the price is negotiable

Rent posts often name no number at all — «توافقی», "negotiable", "price on request". Those used
to be unpublishable: `price` was required, so the only ways through were inventing a number or
dropping the listing. Send `price_negotiable: true` instead and the listing goes live with
**Negotiable** where the rent would be:

```jsonc
// no "price" key at all
"attributesJson": "{\"listing_type\":\"rent\",\"property_type\":\"apartment\",\"bedrooms\":\"2\",\"price_negotiable\":true}"
```

- **It replaces the price, it doesn't accompany it.** Sending both is a `400`
  (`Monthly Rent (CAD) can't have a value when Negotiable price is on.`). Pick one.
- **It is not a fallback for a failed extraction.** Send it only when the post itself says the
  price is open. A post that names a rent your parser couldn't read is still a rejected
  listing, exactly as before — the flag is the publisher's statement, not your parser's.
- **A listing with no number is invisible to price filters.** Someone filtering "under $2,000"
  will not see it. That is deliberate — it has no price to compare — and it is the reason to
  send a real number every time the post gives one.
- Cargo has the same flag under the same key, `price_negotiable` (§7.2). Jobs has no such flag
  (§7.3).

#### Optional attributes

| Key | Type | Allowed values |
|---|---|---|
| `price_negotiable` | boolean | `true` when the post says the price is open. **Mutually exclusive with `price`** — see above |
| `furnishing` | select | `furnished` · `unfurnished` · `partially` |
| `rental_duration` | select | `daily` · `short_term` · `long_term` |
| `area` | number | m² · 5–500 |
| `bathrooms_count` | number | 1–10 · step 0.5 |
| `floor_number` | number | 0–100 |
| `year_built` | select | `0_5` · `5_10` · `10_20` · `20_plus` — an **age bucket**, not a literal year |
| `pets_allowed` | boolean | `true` / `false` |
| `smoking_allowed` | boolean | `true` / `false` |
| `is_owner` | boolean | `true` / `false` |
| `available_from` | date | `YYYY-MM-DD` |
| `amenities` | multi-select | `elevator` · `parking` · `storage` · `balcony` · `terrace` · `garden` · `rooftop` · `security_system` · `cctv` · `doorman` · `renovated` · `kitchen_appliances` · `washing_machine` · `dishwasher` · `air_conditioning` · `heating` · `internet_ready` · `pool` · `sauna` · `gym` |
| `gender_preference` | select | only meaningful for `listing_type: roommate` (not enforced server-side) — `male` · `female` · `family` · `any` |
| `age_range` | range | only meaningful for `listing_type: roommate` — `[from, to]`, bounds 18–70 |
| `lifestyle_tags` | multi-select | only meaningful for `listing_type: roommate` — `quiet` · `early_bird` · `night_owl` · `social` · `party_friendly` · `private` · `student_only` · `professional` · `remote_worker` · `vegetarian` |

### 7.2 Passenger cargo — `passenger-cargo`

A trip between two cities. People travelling and people sending share one pool: the app asks
nobody which side they are on, and the feed shows every listing together. The card and the
detail page are built entirely from these attributes: the route, the date if there is one,
then any optional details the post gave. Neither shows a side. The title isn't shown anywhere.
The description appears on the detail page.

#### Required attributes

| Key | Type | Allowed values |
|---|---|---|
| `origin_country` | select | one of the 48 codes in *Route countries* below |
| `origin` | text | a catalog city of `origin_country` — see *Route cities* below |
| `destination_country` | select | same list as `origin_country` |
| `destination` | text | a catalog city of `destination_country` |

#### Optional attributes

| Key | Type | Allowed values |
|---|---|---|
| `travel_date` | date | `YYYY-MM-DD`, Gregorian. Only a specific date the post states — see *Dates* |
| `role` | select | **Leave it out.** A listing without it is stored as `traveler`, and it is never shown. `traveler` or `sender` is still accepted, so a crawler that already sends it doesn't break |
| `cargo_type` | multi-select | `documents` · `clothes` · `electronics` · `food` · `other`. An array of every kind the post names. A single string is still accepted and stored as a list of one |
| `weight_kg` | number | 0.1–30. Shown as "up to" that weight |
| `price` | number | **per kg** — see *Price and currency* |
| `price_negotiable` | boolean | same rule as housing (§7.1): send it only when the post says the price is negotiable, and never together with `price` |
| `currency` | select | `CAD` · `USD` · `EUR` · `GBP` · `AED` · `CNY` · `TRY` · `RUB`. Send it with every `price`. This list did not grow with the new countries |

A post that names more than one kind of item sends them together. The array lives inside the
`attributesJson` string, like every other attribute:

```jsonc
"cargo_type": ["documents", "clothes"]
```

A post that names one kind can still send `"cargo_type": "documents"`. The server stores that
as a list of one.

#### Route countries

`origin_country` and `destination_country` take one of these codes. The live list is
`GET /api/categories/passenger-cargo/attributes`; if it ever disagrees with this document, the
endpoint is right.

`IR` · `CA` · `US` · `DE` · `IT` · `GB` · `FR` · `AE` · `CN` · `TR` · `RU` · `AM` · `AU` · `AT` ·
`AZ` · `BY` · `BG` · `HR` · `CY` · `CZ` · `DK` · `GE` · `HU` · `IQ` · `IE` · `KZ` · `KG` · `LT` ·
`MY` · `MX` · `MD` · `ME` · `NL` · `NO` · `OM` · `PL` · `QA` · `RO` · `SA` · `RS` · `SG` · `KR` ·
`ES` · `CH` · `TH` · `UA` · `UZ` · `AF`

A country with no city on the list still can't be sent: the country code and the city are
checked separately. Reject a post whose city isn't listed. Don't substitute a nearby city.

#### Route cities

`origin` and `destination` are text fields, but the server only accepts a city from the
location catalog, in the country given by the matching `*_country` attribute:

- Send the catalog `code` or the English `name`, in any case (`tehran` or `Tehran`). The server
  stores the catalog's spelling.
- **Persian names are refused.** The server matches only the code and the English name, so
  `تهران` returns a `400`. Map Persian names yourself.
  `GET /api/locations/cities?countryCode=IR&limit=200` returns every city of **that** country
  with its `code`, `name`, `nameFa` and `provinceCode`. The catalog is 48 countries and 341
  cities. Fetch it at startup, **once per country** from `GET /api/locations/countries`, and
  build a lookup keyed by **country and name**. One call with no `countryCode` does not return
  the whole catalog: the page size caps at 200, and the province lookup behind an unfiltered
  call caps at 200 of 211 provinces, so cities past that cut never appear.
- The same name can exist in two countries: London is in Ontario and in the UK. The country has
  to come from the post. If the post doesn't say, reject the listing.
- If a city isn't on the list, reject the listing. Don't substitute the nearest listed city.
  Log the city names you keep missing and send them to the team. Adding a city is a one-line
  change on our side.

#### The listing's own location is the origin

Send `countryCode` = `origin_country`, `province` = the origin city's `provinceCode`, and
`city` = its `code`. This is the same city as `origin`. Province codes for Iran, Italy and Turkey are
words, not ISO numbers (the endpoint returns e.g. `TEHRAN`, `ALBORZ`). Several of the newer
countries are the same: the code is a readable slug, uppercased on the way out (`VIENNA`,
`CAPITAL-REGION`), not an ISO subdivision. Take `provinceCode` from the city you already
fetched, rather than building it yourself.

#### Dates

The date is optional. Convert Jalali dates (e.g. «۲۰ آبان») to Gregorian. If a post gives no
specific date ("next week", «اواخر آبان», or nothing at all), send the listing **without**
`travel_date`. Don't reject it, and never guess one: a guessed date sends people to the wrong
flight. A date you do send is still checked, and anything that isn't a real `YYYY-MM-DD`
returns a `400`. Skip trips whose date has already passed. The feed hides them, so sending them
only uses up your request budget.

#### Price and currency

- **`price` is per kg.** A listing you send without `role` is stored as `traveler`, and a
  traveller's price is read per kg. If a post's price isn't per kg (a flat fee, or a total for
  one parcel), leave `price` out. The description still shows it. Listings already stored as
  `sender` keep reading as a total.
- **Send `currency` with every `price`, and only a currency the post states.** The app picks a
  currency from the route for its own users, but a figure with no currency is a guess on your
  side, so leave `price` out.
- **A price in any currency outside the list has no field.** That includes toman and rial, and
  it includes the currency of a country that was added without a new `currency` code (an
  Australian dollar, a dram, a forint). Leave `price` out and let the description carry it.
- No `price` and no `price_negotiable` is fine. The card just shows no price.

#### Title and description

Build the title the same way the app does, so crawled and native trips read the same in search
and in the admin review queue: `{origin} → {destination} · {d MMM}`, e.g.
`Toronto → Tehran · 20 Nov`, or `Toronto → Tehran` when there is no date. Keep both city names:
search matches titles. The description is always the post text. §4 explains why.

### 7.3 Jobs — `jobs`

Covers both sides of the market: `role` says whether the listing is a job opening or a person
looking for work. Canada only.

#### Required attributes

| Key | Type | Allowed values |
|---|---|---|
| `role` | select | `employer` — hiring · `seeker` — looking for work |
| `job_category` | select | `restaurant_food` · `construction` · `cleaning` · `delivery_driver` · `retail` · `beauty_personal_care` · `office_admin` · `it_tech` · `general_labour` · `warehouse` · `skilled_trades` · `customer_service` · `sales` · `healthcare_support` · `childcare` · `education` · `design_creative` · `other` |
| `job_type` | select | `full_time` · `part_time` · `contract` · `temporary` |

#### Optional attributes

| Key | Type | Allowed values |
|---|---|---|
| `job_title` | text | e.g. `Line cook`. The card shows it first. Without it, the card shows the field |
| `work_arrangement` | select | `on_site` · `hybrid` · `remote` |
| `salary` | number | CAD · 1–1,000,000 |
| `salary_period` | select | `hour` · `month` · `year` |

#### Rules

- **Canada only.** `countryCode` is `CA`, and `province` and `city` are where the job is (for a
  `seeker`, where they want to work). Reject a post for a job outside Canada. A remote job still
  needs a Canadian city: use the one the post names, and if it names none, reject the post.
- **"Remote" is not a `job_type` value.** "Remote, full-time" is `job_type: full_time` plus
  `work_arrangement: remote`. Sending `job_type: remote` returns a `400`.
- **Removed keys:** `field`, `salary_min`, `salary_negotiable` and `experience_years` no longer
  exist. Any of them in `attributesJson` returns a `400` (unknown key).
- **`job_category: other`** is for a field that genuinely isn't on the list. Don't use it for a
  post your parser couldn't classify. Reject a post whose field you can't determine, the same
  way you would reject a housing post with no price.
- **Pay.** There is no "negotiable" flag. If a post names no pay, or says pay is negotiable,
  send no `salary`. The listing then shows no pay, rather than a "Negotiable" label the poster
  never wrote. Send `salary` only together with `salary_period`, and only when the amount is in
  CAD. If the amount has no period (just "$20") or is in another currency, leave out both
  fields. For a range ("$18–22/hour"), send the low end; the description keeps the full range.

#### Title and description

Build the title the same way the app does:
`{Hiring|Looking for work} · {job_title, or the field's label} · {city}`, e.g.
`Hiring · Line cook · Toronto` or `Looking for work · Restaurant & Food · Vancouver`. Field
labels come from `GET /api/categories/jobs/attributes`. The description is always the post
text. §4 explains why.

### Live schema and validation rules (all journeys)

**Full, live list:** `GET /api/categories`, then `GET /api/categories/{slug}/attributes`
(e.g. `/api/categories/passenger-cargo/attributes`).

> The path segment is the category **slug** (`passenger-cargo`), not the `id` GUID returned by
> `GET /api/categories` — passing the GUID will not match and returns 404.

If this document ever disagrees with that endpoint, **the endpoint is correct.**

Validation rules:
- An unknown key ⇒ the whole listing is rejected. "Send everything and let the server sort it
  out" doesn't work here.
- An empty value (`""` or `null`) ⇒ ignored, not an error. For an optional field you're unsure
  about, either omit it or send it empty.
- Numbers are stored as numbers, not `"2100"`. Both forms are accepted, but send a number.

---

## 8. Location

The country/province/city triple is resolved against a reference catalog:

- The city must actually belong to that province and country.
- Both code and English name are accepted. Persian names are not.
- What's stored is the **canonical form** — `"toronto"` and `"Toronto"` both become `Toronto`,
  otherwise the feed's filters would treat them as two different cities.
- A city that isn't in the catalog ⇒ `400`. Reject the listing.

Coverage today:

| Journey | Countries |
|---|---|
| `housing`, `jobs` | **Canada only, but nationwide.** All 13 provinces and territories are seeded, not just the Toronto area (Toronto/GTA and Vancouver simply get featured UI treatment) |
| `passenger-cargo` | 48 countries, main cities only — the codes in §7.2. 341 cities in all |

Every country other than Canada is only for cargo routes. The server won't stop you: a
housing or jobs listing in Tehran, or in any other catalog city, passes the location check
today and shows up in the feed, next to Canadian listings, in front of an audience that lives
in Canada. Don't send one.

Reference lists:

```http
GET /api/locations/countries
GET /api/locations/provinces?countryCode=CA
GET /api/locations/cities?countryCode=CA&provinceCode=ON
GET /api/locations/cities?q=tehran          # no countryCode: searches every country
```

- The parameter is `provinceCode`, not `province`.
- `q` is a substring match on the code, the English name and the Persian name. It's for
  finding a city, not for validating one.
- A list returns 50 rows unless you pass `limit` (up to 200). Canada has 67 cities, so
  `limit=200` on a **per-country** cities call returns every city of that country. It does not
  return every city in the catalog. `GET /api/locations/countries` returns every country (48
  today). That handler loads up to 200 and does not use the 50-row default.

---

## 9. "Done" checklist

All journeys:

- [ ] `sourceName` is fixed and documented, and no source is shared between a housing channel and a cargo or jobs channel
- [ ] `externalId` comes from a stable identifier (not a hash of the post text)
- [ ] `categorySlug` is `housing`, `passenger-cargo` or `jobs`, never `services` or `social`
- [ ] one of `contactHandle` / `sourceUrl` is always populated
- [ ] `contactHandle` is the **poster's** handle, never the channel's
- [ ] the handle is left empty whenever ownership is uncertain
- [ ] `posterTelegramUserId` is the message author's `from.id`, omitted for channel posts
- [ ] `alreadyExisted: true` is handled as success, not an error
- [ ] every `alreadyExisted: true` on the **first** send of an `externalId` is logged with both ids (§4)
- [ ] `400` does not trigger a retry; only `5xx` does
- [ ] `DELETE` is called when the original post is removed
- [ ] a post missing a required attribute is rejected, not guessed
- [ ] concurrency is capped at 2–3, and all journeys together stay within the daily budget
- [ ] each journey starts with `autoPublish: false`
- [ ] the key lives in an environment variable, not in code

Housing:

- [ ] `price_negotiable` is sent only when the post says the price is open — never to rescue a
      price the parser couldn't read

Cargo:

- [ ] no photos uploaded; `mediaKeys` is `[]`
- [ ] `description` always carries the post text
- [ ] route cities are mapped to catalog codes by country; Persian names never reach the API
- [ ] the lookup is rebuilt from the live catalog, one country at a time, with `limit=200`
- [ ] an unlisted city, or a name the post doesn't tie to a country, rejects the listing
- [ ] `cargo_type`, when the post names kinds of item, is those values; several kinds go in an array
- [ ] the listing's location is the origin city
- [ ] no `role` is sent
- [ ] Jalali dates are converted; a post with no specific date is sent without `travel_date`,
      never with a guessed one; past trips are skipped
- [ ] `price` is per kg, and only with a `currency` the post states that is one of the eight
      codes — a new country's own currency is not on that list

Jobs:

- [ ] no photos uploaded; `mediaKeys` is `[]`
- [ ] `description` always carries the post text
- [ ] `countryCode` is `CA`
- [ ] none of the removed keys (`field`, `salary_min`, `salary_negotiable`, `experience_years`) is sent
- [ ] `remote` goes in `work_arrangement`, never in `job_type`
- [ ] `salary` is sent only in CAD and only with `salary_period`

---

## Examples — with curl

```bash
KEY="…"
BASE_URL="api.advertio.ir"      # a variable, not hardcoded
BASE="https://$BASE_URL"
```

### Housing — one full listing

```bash
SOURCE="telegram-rent"

# 1 — photo
PHOTO=$(curl -sS -X POST "$BASE/api/ingest/media?source=$SOURCE" \
  -H "X-Ingest-Key: $KEY" \
  -F "file=@photo.jpg" | jq -r .key)

# 2 — listing
curl -sS -X POST "$BASE/api/ingest/leads" \
  -H "X-Ingest-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg key "$PHOTO" '{
    sourceName: "telegram-rent",
    externalId: "4021",
    sourceUrl: "https://t.me/some_channel/4021",
    contactHandle: "@landlord",
    title: "Bright 2-bed near Yonge & Eglinton",
    description: "Two bedrooms, 5th floor, available from the 1st",
    categorySlug: "housing",
    attributesJson: ({listing_type:"rent", property_type:"apartment", bedrooms:"2", price:2100} | tostring),
    countryCode: "CA", province: "Ontario", city: "Toronto", neighborhood: "Midtown",
    mediaKeys: [$key],
    autoPublish: false
  }')"

# 3 — later, once the original post is removed
curl -sS -X DELETE "$BASE/api/ingest/leads/$SOURCE/4021" -H "X-Ingest-Key: $KEY"
```

### Cargo — Toronto → Tehran

The post says «۲۰ نوامبر از تورنتو به تهران پرواز دارم، ۱۰ کیلو جا دارم، کیلویی ۱۵ دلار
کانادا». The Persian city names are mapped to catalog codes before sending. There are no photos,
so there is no step 1.

```bash
curl -sS -X POST "$BASE/api/ingest/leads" \
  -H "X-Ingest-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n '{
    sourceName: "telegram-cargo",
    externalId: "8812",
    sourceUrl: "https://t.me/some_cargo_group/8812",
    contactHandle: "@traveller",
    posterTelegramUserId: 123456789,
    title: "Toronto → Tehran · 20 Nov",
    description: "۲۰ نوامبر از تورنتو به تهران پرواز دارم، ۱۰ کیلو جا دارم، کیلویی ۱۵ دلار کانادا",
    categorySlug: "passenger-cargo",
    attributesJson: ({origin_country:"CA", origin:"toronto", destination_country:"IR", destination:"tehran", travel_date:"2026-11-20", weight_kg:10, price:15, currency:"CAD"} | tostring),
    countryCode: "CA", province: "ON", city: "toronto",
    mediaKeys: [],
    autoPublish: false
  }')"
```

### Jobs — an opening in Toronto

```bash
curl -sS -X POST "$BASE/api/ingest/leads" \
  -H "X-Ingest-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n '{
    sourceName: "telegram-jobs",
    externalId: "512",
    sourceUrl: "https://t.me/some_jobs_channel/512",
    contactHandle: "@cafe_owner",
    title: "Hiring · Line cook · Toronto",
    description: "Part-time line cook, evenings, $20/hour, North York location",
    categorySlug: "jobs",
    attributesJson: ({role:"employer", job_category:"restaurant_food", job_type:"part_time", job_title:"Line cook", work_arrangement:"on_site", salary:20, salary_period:"hour"} | tostring),
    countryCode: "CA", province: "ON", city: "toronto",
    mediaKeys: [],
    autoPublish: false
  }')"
```

---

## Further reading

- Full API contract: [`specs/001-housing-paid-contact/contracts/api.md`](../specs/001-housing-paid-contact/contracts/api.md) §`/api/ingest`
- Rationale behind the design decisions: [`docs/adr/ADR-0009`](adr/ADR-0009-external-supply-ingestion.md)
- Why the API moved to its own domain: [`docs/adr/ADR-0012`](adr/ADR-0012-api-subdomain.md)
- Which journeys are live, and why each one stands alone: [`docs/adr/ADR-0001`](adr/ADR-0001-hub-and-independent-journeys.md)
- Code: `backend/src/Advertio.Api/Modules/Lead/LeadIngestEndpoints.cs`
- Every journey's attributes: `backend/src/Modules/Category/Advertio.Modules.Category.Infrastructure/Seed/CategorySeeder.cs`
- The location catalog: `backend/src/Modules/ReferenceData/Advertio.Modules.ReferenceData.Infrastructure/Seed/LocationSeeder.cs`
