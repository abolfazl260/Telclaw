# Telclaw — AI Coding Context

> Purpose: This document is the source of truth for AI-assisted development of Telclaw. Before changing code, read this document and inspect the files named here. Do not infer architecture from filenames alone.

## 1. Project Identity

Telclaw is a Telegram data pipeline. The intended lifecycle is:

```text
Telegram
  ↓
Collection / Crawl
  ↓
Raw SQLite storage
  ↓
Information Processing
  ├─ duplicate detection
  ├─ cleaning
  ├─ normalization
  └─ processing status
  ↓
AI Queue
  ↓
Groq extraction
  ↓
Category validation
  ↓
Processed structured data
  ↓
Future delivery layer
```

The repository is evolving toward separation of collection, processing, AI, storage, UI, and delivery. The existing code is not fully identical to the target architecture in README.md; when making a change, follow the **actual current implementation** on the requested branch and update this document when architecture changes.

## 2. Mandatory Branch Rule

For work requested against a named branch:

1. Work only on that branch.
2. Do not modify `main` unless explicitly requested.
3. Do not create another feature branch unless explicitly requested.
4. Before editing, inspect the current branch version of affected files.
5. After editing, verify imports, call sites, state transitions, and schema compatibility.

Current development branch used by this project:

```text
agent/architecture-refactor
```

## 3. Main Runtime Entry Points

### `main.py`
Application entry point. It starts the application/UI and should remain thin.

### `system_ui.py` / UI modules
Console/TUI interaction, menus, operational commands, and visible progress. UI should call services rather than duplicate business logic.

### `config.py`
Environment/runtime configuration. API keys and secrets must never be hard-coded or committed.

## 4. Collection / Crawl Responsibilities

The crawler is responsible for retrieving Telegram messages and preserving the original information.

Important rule:

**Raw Crawl data is the source of truth for reprocessing.** Cleaning must not overwrite the original message.

Conceptual message data:

```text
channel_username
message_id
sender_id
raw_text / original text
text
media metadata
collection_status
processing_status
ai_status
```

### Crawl duplicate protection

The same Telegram message is identified by its channel and message ID. Existing records for the same `(channel_username, message_id)` must not be inserted again.

This is different from content duplicate detection. Do not confuse:

- **Crawler duplicate:** same Telegram message identity.
- **Processing duplicate:** different messages whose raw content is highly similar for the same sender.

A message without text is not automatically a Crawl duplicate. Media-only messages may still be valid collected records.

## 5. Information Processing Responsibilities

Processing operates on collected records waiting for processing.

Typical state:

```text
collection_status = collected
processing_status = pending
```

### Required processing order

When content-duplicate detection is enabled, the order is:

```text
Raw Crawl data
      ↓
Content duplicate detection
      ↓
Cleaning / normalization
      ↓
Processed record
      ↓
AI queue
```

**Duplicate detection must use the original Crawl data, not `cleaned_text`.**

### Content duplicate rule

Current business rule:

- Compare messages for the same `sender_id`.
- Use the original/raw crawled text as the comparison basis.
- Similarity threshold is 80%.
- At or above 80%, the newer duplicate record is removed.
- The older/original record is retained.
- If sender identity is unavailable, do not perform fuzzy deletion merely from text similarity.

Cleaning must never be used to decide whether two original Telegram messages were duplicates.

### Cleaning / normalization

Cleaning produces `cleaned_text` while preserving raw/original text. It may normalize whitespace, remove irrelevant Telegram noise, and prepare text for downstream AI processing according to the current implementation.

Do not silently change the raw source field.

### Processing states

The exact state names in code are authoritative, but the intended lifecycle is:

```text
pending → processing → processed
                     ↘ failed
```

Failures must remain diagnosable and retryable where supported.

## 6. AI Queue Responsibilities

AI processing consumes records that have successfully passed information processing.

Conceptually:

```text
processing_status = processed
AND
ai_status = pending
```

Messages with no usable text are skipped by AI processing as `no_text` rather than sent to the provider.

Provider permission/authentication failures must be reported clearly. A provider permission failure may stop the queue while leaving remaining records pending.

## 7. AI Provider — Groq

Current provider:

```text
Provider: Groq
Endpoint: https://api.groq.com/openai/v1/chat/completions
```

The application uses an OpenAI-compatible HTTP request.

Current extraction request uses JSON object mode rather than the previous strict JSON Schema mode because strict structured output caused provider validation problems for the selected model. Do not re-enable JSON Schema blindly; test the selected model first.

### Important provider debugging rule

If Python HTTP fails but system `curl` succeeds against the same endpoint/key/model, investigate the Python HTTP transport before changing the model. In the project's recent debugging, `urllib` returned HTTP 403 while system curl returned HTTP 200, proving that the endpoint/model/key could work outside the Python transport.

The current extractor uses `requests`; therefore `requests` must be declared in project dependencies and the runtime environment must install it.

## 8. AI Prompt Contract

The extraction prompt is built by the AI extractor and receives the processed Telegram text as the user input.

The prompt must require:

- Category classification returns exactly one of: `housinglist`, `transferlist`, `joblist`, or `none`.
- Category-specific extraction remains limited to extractable categories: `housinglist`, `transferlist`, or `joblist`.
- Extraction only from facts supported by the message.
- No invented values.
- Unknown scalar values as `null`.
- Unknown list values as `[]` where appropriate.
- English field names.
- Valid JSON only.
- No Markdown fences.
- No explanations/comments.
- No `<think>` tags in the returned JSON.

### Title contract

`title` must always be in English.

If the source is not English:

```text
source language → natural English title
```

Do not transliterate. Do not return the original-language title. Keep the title concise and marketplace-appropriate. Do not put URLs, hashtags, emojis, or explanations in the title.

There is also local validation after the provider response. Non-Latin scripts commonly used for Persian/Arabic, Cyrillic, Greek, CJK, or Korean are rejected for the title.

### Currency contract

The platform's canonical currency is **Canadian dollars (CAD)** for all requests/listings.

- Housing: `currency = CAD` when a valid monetary value is available.
- Transfer: `currency = CAD` when a valid monetary value is available.
- Job: `salary_currency = CAD` when a valid salary value is available.
- Do not invent exchange rates.
- If the source currency is not safely convertible from information available to the system, do not fabricate a CAD amount; use the schema's null/unknown behavior.
- Never treat USD/EUR/GBP/TRY/IRR/etc. as the platform's canonical output currency.

The extractor also normalizes returned currency fields to CAD where a monetary field is present. This is a business-rule normalization, not a currency conversion engine.


## AI Category Classification Queue

After deterministic cleaning/normalization succeeds, messages enter an independent AI category classification queue. Classification is intentionally separate from category-specific extraction so the project can batch lightweight category decisions before running more expensive structured extraction.

Default classification batch size is configured with `TELCLAW_AI_CLASSIFICATION_BATCH_SIZE` and defaults to `50`. Each AI request receives an array of cleaned messages with `message_id` and `text`, and returns one category per message. Valid classification categories are:

```text
housinglist
transferlist
joblist
none
```

Records classified as `none` are marked as skipped for extraction. Other records are moved to the future category-specific extraction queue by setting their AI status to pending.

## 9. AI Category Schema

Current schema definitions live in:

```text
ai/category_schemas.py
```

Current categories:

```text
housinglist
transferlist
joblist
```

### Housing fields

```text
property_type
listing_type
title
description
location
price
currency
rent_period
bedrooms
bathrooms
area
area_unit
furnished
availability
property_condition
contact
features
```

### Transfer fields

`transferlist` is for air-cargo / passenger-baggage shipping requests.

```text
title
description
origin_city
origin_province
origin_country
destination_city
destination_province
destination_country
airline
flight_number
departure_date
departure_time
arrival_date
arrival_time
transport_type
cargo_type
weight
weight_unit
quantity
price
currency
contact
features
```

### Job fields

```text
job_title
company
location
employment_type
salary
salary_currency
salary_period
experience
education
skills
remote
job_type
description
application_method
contact
```

`CATEGORY_FIELDS` is the field allow-list. `validate_result()` rejects unsupported categories/data shapes and keeps only allowed fields.

`build_json_schema()` exists for structured-output use, but the current extractor does not blindly send it to Groq. If structured outputs are reintroduced, test compatibility with the selected model and preserve the category contract.

## 10. AI Output Lifecycle

Current conceptual sequence:

```text
Groq response
   ↓
HTTP/provider validation
   ↓
JSON parsing
   ↓
Business normalization
   ├─ currency normalization
   └─ other deterministic rules
   ↓
English title validation
   ↓
category_schemas.validate_result()
   ↓
AI result persistence
```

Provider output must never be trusted simply because it is valid JSON.

## 11. Storage Rules

SQLite is the primary persistence layer.

The raw message must remain available after processing so that cleaning, deduplication, prompts, or AI rules can be changed and records reprocessed.

When a message record is deleted, dependent AI/category records may be removed through foreign-key cascade. Any new deletion rule must explicitly consider downstream data loss.

## 12. What to Change When the User Gives a New Requirement

Use this decision process before editing:

### A. Requirement concerns Telegram collection
Inspect:

```text
collection/
Telegram session modules
storage/database.py or database layer
channel configuration
```

Check message identity, media handling, collection status, and re-crawl behavior.

### B. Requirement concerns cleaning, normalization, or duplicates
Inspect:

```text
processing/
message repository/storage layer
processing status transitions
```

Preserve raw data. Verify whether the requirement belongs before or after cleaning. For duplicate detection based on original Telegram content, it must happen before cleaning.

### C. Requirement concerns AI extraction/prompt
Inspect:

```text
ai/extractor.py
ai/ai_service.py
ai/category_schemas.py
```

Also inspect the AI queue and persistence code because changing the output contract can affect storage.

### D. Requirement concerns a category field
Inspect:

```text
ai/category_schemas.py
ai/extractor.py
AI persistence tables/repositories
```

Do not add a field only to the prompt. The field must exist in the schema/allow-list and survive validation/storage.

### E. Requirement concerns title/language/currency/business rules
Prefer deterministic validation/normalization in code **in addition to** the prompt. Prompts are instructions; code-level business rules are enforcement.

### F. Requirement concerns UI/menu/test tools
Inspect:

```text
system_ui.py / UI modules
main.py
service layer called by the menu
```

Do not duplicate provider logic inside UI. UI should invoke the same service used by production processing.

### G. Requirement concerns provider/model/API errors
Inspect:

```text
ai/extractor.py
config.py
requirements.txt
.env.example
```

Test the exact endpoint, model, API key availability, HTTP transport, request format, and response format before changing business logic.

## 13. Change Impact Matrix

| Requirement | Primary files | Also verify |
|---|---|---|
| Crawl behavior | `collection/*` | DB, channel manager, status |
| Raw message fields | DB/storage + collection | migrations, repositories |
| Cleaning | `processing/*` | raw preservation, status |
| Duplicate rule | `processing/*` | raw text, sender ID, deletion cascade |
| AI prompt | `ai/extractor.py` | schema, validator, persistence |
| Housing field | `ai/category_schemas.py` | prompt, persistence |
| Transfer field | `ai/category_schemas.py` | prompt, persistence |
| Job field | `ai/category_schemas.py` | prompt, persistence |
| Title language | extractor + validation | schema, persistence |
| Currency | extractor + deterministic normalization | schema, persistence |
| Groq model | `config.py` + extractor | provider compatibility/test |
| Groq connection test | UI + AI/provider service | same HTTP path as production |
| AI queue | `ai/ai_service.py` | DB statuses, retries |
| Delivery | `delivery/*` | API contract, retry/idempotency |
| Menu/TUI | UI modules | service layer |

## 14. Rules for Future AI-Assisted Changes

When a user says something like:

> "Add X to housinglist"

Do **not** change only the prompt. Trace the complete contract:

```text
source message
 → prompt
 → AI output
 → schema
 → validator
 → persistence
 → downstream delivery
```

When a user says:

> "AI should always return X in English/CAD/etc."

Implement both:

```text
Prompt instruction
+
Deterministic application-side validation/normalization
```

When a user says:

> "Delete duplicates"

First establish the identity basis and deletion scope. Never use fuzzy text matching without a clear business key. For the current rule, the key is `sender_id` + raw original text similarity, with an 80% threshold and the newer duplicate removed.

When a user reports an error, reproduce the exact path before changing architecture. Distinguish:

```text
transport error
provider authentication/permission error
model availability error
request-format error
JSON/schema validation error
local validation error
database/storage error
```

## 15. Documentation Maintenance Rule

This document must be updated whenever a change materially alters:

- pipeline order
- database/message state machine
- AI provider/model/request format
- category fields/schema
- prompt contract
- deterministic business rules
- duplicate/deletion behavior
- UI-to-service execution path
- external delivery contract

The goal is that a future AI assistant can read `docs/AI_CODING_CONTEXT.md`, inspect the referenced current files, and identify the complete change surface instead of modifying one file in isolation.

## 16. Current Known Constraints

1. Raw Telegram content must remain recoverable.
2. Duplicate detection is based on original Crawl content, not cleaned content.
3. Current fuzzy duplicate threshold is 80% for the same sender.
4. Title output is English.
5. Canonical platform currency is CAD.
6. AI output is JSON and is locally validated.
7. Groq provider behavior must be tested with the exact selected model/request format.
8. `requests` is a runtime dependency for the current extractor.
9. Secrets must stay in environment variables.
10. `main` is not to be changed when work is requested on a feature/refactor branch.
