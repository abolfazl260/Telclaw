# Telclaw — System Structure & Database Map

> **Purpose:** This document is the navigation map for AI-assisted development of Telclaw. An AI agent should read this file **before modifying code**, then inspect the exact files listed for the requested change. Do not infer behavior from filenames alone.
>
> **Current branch:** `Stage`
>
> **Repository:** `abolfazl260/Telclaw`

---

## 1. Source-of-Truth Rules

When investigating or changing Telclaw, use this priority:

1. Current code on the requested branch (`Stage` when no other branch is specified).
2. `storage/database.py` and repository methods for actual persistence behavior.
3. `ai/category_schemas.py` for the AI field allow-list and validation contract.
4. `ai/prompts/*.txt` for extraction instructions.
5. Services/workers for queue state transitions.
6. Delivery code and integration documents for publish behavior.
7. Documentation for architecture/context, but documentation must be corrected if it conflicts with the current implementation.

**Never modify only a prompt, schema, UI, or database field in isolation when a requirement crosses multiple boundaries. Trace the complete data path first.**

---

## 2. High-Level System

Telclaw is a staged Telegram data pipeline:

```text
Telegram
   │
   ▼
Collection / Crawl
   │
   │  raw Telegram message
   ▼
SQLite: messages
   │
   ▼
Processing Queue
   │
   ├── duplicate detection
   ├── cleaning
   └── normalization
   │
   ▼
Processed message
   │
   ├─────────────────────────────┐
   ▼                             ▼
AI Classification Queue       other DB/service operations
   │
   ▼
Category selected
   │
   ├── housinglist
   ├── transferlist
   ├── joblist
   └── none
   │
   ▼
Category-specific AI Extraction
   │
   ▼
JSON validation + deterministic normalization
   │
   ▼
SQLite category table
   │
   ├── housinglist
   ├── transferlist
   └── joblist
   │
   ▼
Delivery
   ├── Telegram transfer publisher
   └── Advertio integration
```

The important architectural principle is that **collection, processing, classification, extraction, and delivery are separate stages** even though some orchestration currently exists in shared services.

---

## 3. Repository Structure

```text
Telclaw/
│
├── main.py                         # Application entry point / orchestration
├── config.py                       # Environment and runtime configuration
├── database.py                     # Legacy/compatibility database wrapper
├── scheduler.py                    # Scheduler entry point
├── sessions_manager.py             # Telegram session/account management
├── system_ui.py                    # Main TUI/system interface
├── ui.py                           # Additional UI helpers
├── error_handler.py                # Central error handling
├── channels.json                   # Channel configuration data
├── requirements.txt                # Runtime dependencies
│
├── collection/                     # Telegram collection boundary
│   ├── crawler.py                  # Telegram crawl implementation
│   ├── collector_service.py        # Collection service wrapper
│   └── media_downloader.py         # Media download logic
│
├── processing/                     # Deterministic processing boundary
│   ├── processing_service.py       # Main processing workflow
│   ├── cleaner.py                  # Text cleaning
│   ├── normalizer.py               # Text/data normalization
│   ├── duplicate_detector.py       # Duplicate/content detection helpers
│   ├── classifier.py               # Processing classification helper
│   ├── property_extractor.py       # Legacy/specialized extraction helper
│   ├── contracts.py                # Processing contracts
│   ├── stages.py                   # Processing stage definitions
│   └── run_processing.py           # Processing runner
│
├── ai/                             # AI boundary
│   ├── ai_service.py               # Main AI orchestration
│   ├── classification_service.py   # Category classification queue/workflow
│   ├── category_classifier.py      # Classification implementation
│   ├── extraction_service.py       # Category extraction service wrapper
│   ├── extractor.py                # Provider-facing extraction + parsing
│   ├── category_schemas.py         # Field allow-list + result validation
│   ├── prompt_loader.py             # Prompt loading + current-date injection
│   ├── provider_manager.py         # Provider abstraction/failover orchestration
│   ├── provider_failover.py        # Failover support
│   ├── rate_limiter.py             # AI rate limiting
│   ├── providers/
│   │   ├── base.py                 # Provider interface
│   │   ├── factory.py              # Provider creation
│   │   ├── groq.py                 # Groq implementation
│   │   └── cloudflare.py            # Cloudflare implementation
│   └── prompts/
│       ├── housinglist.txt         # Housing extraction contract
│       ├── transferlist.txt        # Transfer extraction contract
│       └── joblist.txt             # Job extraction contract
│
├── storage/                        # SQLite persistence boundary
│   ├── database.py                 # Schema, migrations, CRUD, queue queries
│   ├── message_repository.py       # Repository boundary used by workers
│   ├── content_duplicate.py        # Content duplicate support
│   ├── location_normalizer.py      # AI-derived canonical location normalization
│   └── __init__.py                 # Storage initialization compatibility hooks
│
├── services/                       # Application/service orchestration
│   ├── crawler_service.py          # Crawl orchestration
│   ├── collector_service.py        # Collection-related service
│   ├── message_service.py          # Message operations
│   ├── processing_service.py       # Processing orchestration
│   ├── processing_runner.py        # Processing worker runner
│   ├── account_service.py          # Telegram account operations
│   ├── channel_service.py          # Channel operations
│   ├── crawl_job_service.py        # Crawl job operations
│   ├── scheduler_service.py        # Scheduled jobs
│   └── stage_control.py            # Stage controls
│
├── delivery/                       # Output/publishing boundary
│   ├── telegram_transfer.py        # Telegram transfer delivery helpers
│   ├── telegram_transfer_publisher.py # Transfer publisher
│   ├── advertio_client.py           # Advertio API client
│   └── advertio_service.py          # Advertio delivery orchestration
│
├── monitoring/
│   └── telegram_monitor.py         # Telegram monitoring/subscriber notifications
│
├── docs/                           # Architecture and integration documentation
├── tests/                           # Automated tests
└── TelegramProcessor/              # Older/legacy processing implementation
```

### Legacy warning

`TelegramProcessor/` is a separate older implementation. For changes to the current Stage pipeline, prefer the top-level `collection/`, `processing/`, `ai/`, `storage/`, `services/`, and `delivery/` packages unless code inspection proves that the legacy implementation is still part of the requested execution path.

---

## 4. Where to Start When Investigating a Requirement

### A. Telegram crawl / receive problem

Inspect in this order:

```text
main.py
→ services/crawler_service.py
→ collection/collector_service.py
→ collection/crawler.py
→ storage/message_repository.py
→ storage/database.py
```

Also inspect `sessions_manager.py` and `services/account_service.py` when the issue concerns logged-in Telegram accounts.

Check:

- channel selection
- date range
- crawl mode
- media detection
- sender type
- Telegram message identity
- duplicate insertion
- collection status

---

### B. Processing / cleaning problem

Inspect:

```text
services/processing_service.py
→ processing/processing_service.py
→ processing/cleaner.py
→ processing/normalizer.py
→ processing/duplicate_detector.py
→ storage/message_repository.py
→ storage/database.py
```

Important rule:

```text
raw_text/original text = source of truth
cleaned_text           = derived data
```

Do not overwrite raw Telegram content to implement a cleaning rule.

For content duplicates, verify the exact business key and comparison source before changing deletion behavior.

---

### C. AI category classification problem

Inspect:

```text
ai/classification_service.py
→ ai/category_classifier.py
→ ai/provider_manager.py
→ ai/providers/*
→ storage/message_repository.py
→ storage/database.py
```

The classification result is one of:

```text
housinglist
transferlist
joblist
none
```

Classification is separate from category-specific extraction.

---

### D. AI extraction problem

Inspect:

```text
ai/ai_service.py
→ ai/extraction_service.py
→ ai/extractor.py
→ ai/prompt_loader.py
→ ai/prompts/<category>.txt
→ ai/category_schemas.py
→ storage/message_repository.py
→ storage/database.py
```

Then inspect the relevant delivery module if the extracted field is displayed or published.

A field is not fully implemented until it survives this path:

```text
Telegram message
→ cleaned text
→ AI prompt
→ provider response
→ JSON parsing
→ schema validation
→ deterministic normalization
→ database persistence
→ delivery/output
```

---

### E. AI provider/API problem

Inspect:

```text
config.py
→ ai/provider_manager.py
→ ai/provider_failover.py
→ ai/providers/base.py
→ ai/providers/groq.py / cloudflare.py
→ ai/extractor.py
→ requirements.txt
```

Separate these failure classes:

```text
network/transport
authentication
permission
rate limit
model availability
request format
response format
JSON parsing
schema validation
local business validation
```

Do not change prompts or database schema to solve a transport/provider error.

---

### F. Delivery / Telegram publishing problem

Inspect:

```text
delivery/telegram_transfer_publisher.py
→ delivery/telegram_transfer.py
→ storage/message_repository.py
→ storage/database.py
```

For Advertio:

```text
delivery/advertio_service.py
→ delivery/advertio_client.py
→ docs/ADVERTIO_INGEST_INTEGRATION.md
→ docs/integrations/advertio-ingest-api.md
```

Verify idempotency, delivery status, media lifecycle, and retry behavior before changing publishing code.

---

### G. Queue/status/TUI problem

Inspect:

```text
system_ui.py / ui.py
→ services/*
→ storage/message_repository.py
→ storage/database.py
```

The UI must not invent queue counts. Queue/status displays should use the same database-backed state used by workers.

---

## 5. SQLite Database

### Database access

The primary implementation is:

```text
storage/database.py
```

Connections are created by `get_connection()` and use SQLite row objects plus foreign keys.

The actual database file path comes from:

```text
config.DB_NAME
```

Do not assume a fixed filename when debugging a deployment. Inspect `config.py` and the environment.

---

## 6. Core Table: `messages`

`messages` is the central table. Every Telegram message enters the pipeline here before category-specific data is stored.

### Identity

```text
id                 INTEGER PRIMARY KEY AUTOINCREMENT
channel_username   TEXT NOT NULL
message_id         INTEGER NOT NULL
UNIQUE(channel_username, message_id)
```

Therefore:

- the same Telegram message in the same channel must not be inserted twice;
- `channel_username + message_id` is the Telegram message identity key;
- this is different from content-duplicate detection.

### Source/raw content

```text
text
raw_text
cleaned_text
```

Interpretation:

- `text`: original/current message text field.
- `raw_text`: preserved source content for reprocessing.
- `cleaned_text`: derived processing output.

Never use `cleaned_text` as a replacement for the raw source when implementing duplicate detection or future reprocessing rules.

### Telegram metadata

```text
channel_id
channel_name
sender_id
sender_username
sender_type
has_media
media_type
file_unique_id
media_path
message_link
media_reference
date
```

The crawler is responsible for collecting these values when available.

### Pipeline state

```text
collection_status
processing_status
classification_status
ai_status
```

These are independent stage states.

Conceptual lifecycle:

```text
collection_status = collected
        ↓
processing_status = pending
        ↓
processing_status = processing
        ↓
processing_status = processed
        ↓
classification_status = pending
        ↓
classification_status = processing
        ↓
classification_status = processed
        ↓
ai_status = pending
        ↓
ai_status = processing
        ↓
ai_status = processed
```

Failure states exist per stage.

### Classification fields

```text
classification_category
classification_error
classification_processed_at
classification_attempts
```

`classification_category` records the category selected by the classification stage.

### AI fields

```text
ai_category
ai_processed_at
ai_error
pipeline_version
cleaned_at
```

Current repository behavior intentionally removes the provider error from `ai_error` on failed AI attempts rather than storing provider failure text in the message record.

### Advertio fields

```text
advertio_status
advertio_lead_id
advertio_error
advertio_processed_at
```

These track external delivery independently from AI processing.

---

## 7. `messages` Indexes

Current database code creates indexes for frequently queried dimensions:

```text
idx_messages_date
idx_messages_channel_date
idx_messages_collection_status
idx_messages_processing_status
idx_messages_classification_status
idx_messages_ai_status
idx_messages_media_type
idx_messages_ai_category
idx_messages_sender_id
idx_messages_advertio_status
```

When adding a new high-volume queue/filter query, inspect whether an index is required before changing query logic.

---

## 8. Category Tables

All category tables use the same structural pattern:

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
processed_message_id INTEGER NOT NULL UNIQUE
<category-specific fields>
created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE
```

This means:

- one category record belongs to one processed message;
- the same message cannot have two rows in the same category table;
- deleting the parent message can cascade into the category record;
- category persistence must be treated as downstream data.

---

## 9. `housinglist`

Current category fields:

```text
property_type
listing_type
title
description
location
country_code
province
city
neighborhood
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

AI allow-list source:

```text
ai/category_schemas.py → CATEGORY_FIELDS["housinglist"]
```

Important current business rule in `category_schemas.py`:

- housing location is currently normalized with Canada-oriented deterministic logic;
- `country_code` is normalized toward `CA` in that housing-specific helper;
- known Canadian cities/provinces/neighborhoods may be inferred by deterministic application logic.

Do not generalize this housing rule to transfer locations without inspecting the transfer location path.

---

## 10. `transferlist`

`transferlist` represents transfer/shipping/passenger-baggage requests.

Current AI fields:

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
transfer_role
cargo_type
weight
weight_unit
quantity
volume
volume_unit
price
currency
contact
features
```

`transfer_role` is intended to be:

```text
passenger
shipper
```

The Telegram publisher converts these to user-facing labels such as:

```text
#مسافر
#ارسال_کننده
```

The publisher also formats ISO country codes into country flags for display. Flags are a presentation concern; country codes stored by AI/storage should remain plain values.

### Transfer location normalization

`storage/message_repository.py` maintains a separate table:

```text
transfer_locations
```

This table stores canonical reporting values:

```text
origin_city_canonical
destination_city_canonical
origin_city_key
destination_city_key
origin_country_iso2
destination_country_iso2
```

The canonicalization path is:

```text
AI transfer extraction
→ origin/destination city + country
→ storage.location_normalizer.normalize_location()
→ transfer_locations
```

No UN/LOCODE database is required by the current design.

### Important schema compatibility detail

`ai/category_schemas.py` declares `transfer_role`, while `storage/__init__.py` injects `transfer_role` into the database category schema before initialization/migration. This compatibility hook exists because older SQLite databases may not contain the column.

When changing `transferlist` fields, inspect all of these together:

```text
ai/category_schemas.py
ai/prompts/transferlist.txt
storage/database.py
storage/__init__.py
storage/message_repository.py
delivery/telegram_transfer_publisher.py
tests/test_transferlist_schema.py
```

---

## 11. `joblist`

Current fields:

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

AI allow-list source:

```text
ai/category_schemas.py → CATEGORY_FIELDS["joblist"]
```

---

## 12. `transfer_locations`

Created by:

```text
storage/message_repository.py
```

Schema:

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
processed_message_id INTEGER NOT NULL UNIQUE
origin_city_canonical TEXT
origin_city_key TEXT
origin_country_iso2 TEXT
destination_city_canonical TEXT
destination_city_key TEXT
destination_country_iso2 TEXT
created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
```

Foreign key:

```text
processed_message_id → messages.id
ON DELETE CASCADE
```

Indexes:

```text
idx_transfer_locations_origin
idx_transfer_locations_destination
```

Purpose:

- reporting/filtering by canonical route;
- deterministic ASCII-safe city keys;
- ISO-2 country representation;
- separation between display/source extraction and canonical route reporting.

The current design explicitly does **not** use UN/LOCODE.

---

## 13. Other SQLite Tables

### `crawler_settings`

```text
channel_username TEXT PRIMARY KEY
target_date TEXT NOT NULL
last_crawled_date TEXT
```

Used for crawler/channel crawl state.

### `telegram_monitor_subscribers`

```text
chat_id INTEGER PRIMARY KEY
username
first_name
enabled INTEGER NOT NULL DEFAULT 1
first_seen
last_seen
```

Used by Telegram monitoring/subscriber notifications.

---

## 14. Database Migration Rules

`storage/database.py` performs schema initialization and migration.

For `messages`:

```text
PRAGMA table_info(messages)
→ add missing columns with ALTER TABLE
→ normalize old state values
```

For category tables:

- missing tables are created;
- missing fields are added;
- `transferlist` has additional rebuild compatibility logic when its declared schema differs from the expected schema.

**Never solve a schema error by deleting the SQLite database unless the user explicitly requests data destruction.**

When adding a required field:

```text
1. AI allow-list/schema
2. prompt
3. DB schema/migration
4. persistence code
5. read/query code
6. downstream publisher/API
7. tests
```

---

## 15. Queue State Map

### Processing queue

A message is eligible for processing when approximately:

```text
collection_status = collected
AND
processing_status = pending
```

### Classification queue

A message is eligible when approximately:

```text
processing_status = processed
AND
classification_status = pending
```

### Category extraction / AI queue

For classified categories other than `none`:

```text
processing_status = processed
AND
ai_status = pending
```

### Advertio queue

Current status logic considers AI-processed records with:

```text
advertio_status IN (waiting, retry)
AND
ai_status = processed
```

Do not invent a new queue status in the UI without checking the worker's actual query.

---

## 16. AI Provider Architecture

The provider layer is intentionally replaceable.

```text
AI Service
   ↓
Provider Manager
   ↓
Provider Failover / Rate Limiter
   ↓
Provider Interface
   ├── Groq
   └── Cloudflare
```

Important files:

```text
ai/provider_manager.py
ai/provider_failover.py
ai/providers/base.py
ai/providers/factory.py
ai/providers/groq.py
ai/providers/cloudflare.py
ai/rate_limiter.py
```

Application/business logic should call the provider abstraction rather than directly depending on one provider implementation.

---

## 17. Prompt and Current-Date Rules

Prompts live in:

```text
ai/prompts/
```

`ai/prompt_loader.py` appends the current system date to the rendered prompt.

Therefore relative-date extraction should be evaluated against the runtime date, not an old model-training date.

For transfer extraction, inspect:

```text
ai/prompts/transferlist.txt
ai/prompt_loader.py
```

The transfer prompt currently contains rules for interpreting dates without a year relative to the current/recent calendar period.

If changing date semantics, inspect both prompt and application-side validation/normalization. Do not assume a prompt alone enforces a business rule.

---

## 18. Location Rules

### General transfer principle

For transfer origin/destination locations:

```text
AI extraction
→ city in canonical English/Roman form
→ country as ISO-3166-1 alpha-2 code
→ deterministic storage normalization
```

Examples:

```text
Hannover → DE
Tehran   → IR
Stuttgart → DE
Istanbul → TR
Toronto  → CA
London   → GB
Dubai    → AE
```

Stored AI fields must not contain flags/emojis. Flags belong to presentation code.

### No UN/LOCODE

The current implementation does not use a UN/LOCODE database. Do not add one unless explicitly requested.

---

## 19. Delivery Architecture

### Telegram transfer publisher

Main file:

```text
delivery/telegram_transfer_publisher.py
```

Responsibilities include:

- select eligible transfer records;
- format origin/destination;
- display country flags;
- display passenger/shipper role;
- omit the transfer title from the Telegram output when configured by current implementation;
- display structured transfer information;
- publish through Telegram;
- update delivery state.

If output formatting changes, inspect both the publisher and the transfer schema/persistence because missing stored values cannot be fixed only in presentation code.

### Advertio

Main files:

```text
delivery/advertio_client.py
delivery/advertio_service.py
docs/ADVERTIO_INGEST_INTEGRATION.md
docs/integrations/advertio-ingest-api.md
```

Media should be downloaded/dealt with at the delivery stage when required, rather than forcing every crawled image to be downloaded immediately.

---

## 20. Media Lifecycle

Conceptual flow:

```text
Telegram message
→ media metadata/reference collected
→ media URL/reference persisted
→ do not necessarily download immediately
→ download when delivery/publishing requires it
→ publish
→ cleanup when appropriate
```

When changing media behavior, inspect:

```text
collection/media_downloader.py
storage/database.py
storage/message_repository.py
ai/ai_service.py
 delivery/advertio_service.py
delivery/telegram_transfer_publisher.py
tests/test_ai_media_downloader_wiring.py
tests/test_advertio_media_cleanup.py
```

Do not introduce eager downloading just to make an AI or delivery feature easier unless explicitly required.

---

## 21. Duplicate Detection

There are two different duplicate concepts.

### Telegram identity duplicate

```text
channel_username + message_id
```

This is enforced by the database unique constraint.

### Content duplicate

This is a business-level processing rule and must not be confused with Telegram identity.

Current documented rule:

```text
same sender_id
+
original/raw message content similarity
```

The processing duplicate detector must be inspected before changing the threshold or deletion behavior.

Do not use cleaned text as the source for original-content duplicate detection.

---

## 22. State/Status Ownership

The following principle should be maintained:

```text
Worker/service = owns state transition
Database       = persists state
UI             = reads/displays state
```

Avoid this anti-pattern:

```text
UI calculates a queue state differently from the worker
```

If a queue says one number in the UI and another in worker logs, inspect the SQL predicates and state transitions first.

---

## 23. Change Impact Matrix

| Requirement | Start here | Must also inspect |
|---|---|---|
| Crawl filtering | `collection/crawler.py` | crawler service, DB, channels |
| Crawl date range | `collection/crawler.py` | scheduler, settings, DB |
| Sender filtering | `collection/crawler.py` | message repository, tests |
| Telegram duplicate | `storage/database.py` | crawler, repository |
| Content duplicate | `processing/duplicate_detector.py` | raw text, sender ID, deletion path |
| Cleaning | `processing/cleaner.py` | processing service, raw preservation |
| Normalization | `processing/normalizer.py` | downstream AI/schema |
| Processing queue | `processing/processing_service.py` | DB state queries, UI |
| AI classification | `ai/classification_service.py` | category classifier, provider manager, DB |
| AI extraction | `ai/extractor.py` | prompt, schema, persistence |
| AI prompt | `ai/prompts/*` | prompt loader, validator |
| New category field | `ai/category_schemas.py` | prompt, DB, repository, delivery |
| Transfer location | `ai/prompts/transferlist.txt` | location normalizer, transfer_locations, publisher |
| Transfer role | `category_schemas.py` | DB migration, repository, publisher, tests |
| Date interpretation | `transferlist.txt` | prompt loader, extractor/validator |
| Country flag | transfer publisher | stored country value / location normalizer |
| AI provider error | provider layer | config, extractor, dependency versions |
| Queue count | DB query | worker query + UI |
| Advertio delivery | `delivery/advertio_service.py` | client, integration docs, DB |
| Telegram delivery | `delivery/telegram_transfer_publisher.py` | transfer schema/repository |
| Media | `collection/media_downloader.py` | AI wiring, delivery, cleanup tests |
| Schema error | `storage/database.py` | migrations, `storage/__init__.py`, repository |

---

## 24. Mandatory Investigation Checklist for AI Agents

Before making a code change:

```text
[ ] Confirm requested branch.
[ ] Read this document.
[ ] Locate the actual entry point for the affected feature.
[ ] Inspect the service/worker that owns the behavior.
[ ] Inspect the database query/state transition.
[ ] Inspect schema/allow-list if data fields are involved.
[ ] Inspect prompt if AI behavior is involved.
[ ] Inspect persistence if a field is added/changed.
[ ] Inspect delivery if the field is displayed/published.
[ ] Inspect relevant tests.
[ ] Check migration compatibility with existing SQLite databases.
[ ] Avoid unrelated refactors.
[ ] Preserve raw Telegram data.
[ ] Do not add UN/LOCODE unless explicitly requested.
[ ] Do not put provider/model-specific logic into unrelated business modules.
[ ] After editing, verify imports, call sites, state transitions, and schema compatibility.
```

---

## 25. Common Mistakes to Avoid

### Mistake 1 — Editing only the prompt

A prompt field that is not in `CATEGORY_FIELDS`, database schema, and persistence code may be discarded or never stored.

### Mistake 2 — Editing only the database

Adding a DB column without updating the AI allow-list/prompt/persistence/downstream consumer produces an unused field.

### Mistake 3 — Fixing UI instead of the worker

If queue counts are wrong, inspect the worker and SQL predicates before changing the display.

### Mistake 4 — Replacing raw text with cleaned text

This destroys the ability to reprocess historical Telegram messages correctly.

### Mistake 5 — Confusing Telegram duplicates with content duplicates

`channel_username + message_id` is message identity. Content similarity is a separate business rule.

### Mistake 6 — Treating flags as stored data

Country flags are presentation. Stored country values should remain canonical/parseable values such as `DE`, `IR`, `CA`.

### Mistake 7 — Assuming an old SQLite database has the current schema

Always inspect migrations and `PRAGMA table_info(...)` when a `no such column` error appears.

### Mistake 8 — Adding external location databases unnecessarily

The current transfer architecture uses AI + deterministic normalization. Do not reintroduce UN/LOCODE infrastructure without explicit approval.

### Mistake 9 — Assuming provider errors are business-logic errors

First determine whether the failure is transport, authentication, permission, rate limit, model, request-format, JSON, or schema validation.

---

## 26. Minimal Debugging Commands

On the server, first identify the project and branch:

```bash
cd /opt/Crawlerbot/Telclaw
git status
git branch --show-current
git log -1 --oneline
```

Inspect the SQLite schema without changing data:

```bash
sqlite3 <DB_FILE> ".tables"
sqlite3 <DB_FILE> "PRAGMA table_info(messages);"
sqlite3 <DB_FILE> "PRAGMA table_info(transferlist);"
sqlite3 <DB_FILE> "PRAGMA table_info(transfer_locations);"
```

Inspect queue state:

```sql
SELECT collection_status, processing_status, classification_status, ai_status,
       COUNT(*)
FROM messages
GROUP BY collection_status, processing_status, classification_status, ai_status;
```

Inspect transfer rows:

```sql
SELECT * FROM transferlist ORDER BY id DESC LIMIT 10;
```

Inspect canonical transfer locations:

```sql
SELECT * FROM transfer_locations ORDER BY id DESC LIMIT 10;
```

**Never run destructive SQL during diagnosis.**

---

## 27. Documentation Maintenance

Update this file when a change materially alters:

- directory responsibilities;
- pipeline order;
- queue/state machine;
- database schema;
- category fields;
- AI provider architecture;
- prompt contracts;
- location/date/currency rules;
- duplicate behavior;
- media lifecycle;
- delivery behavior;
- UI-to-service execution paths.

The objective is not to describe every function. The objective is to give an AI agent a reliable **map of where to investigate** so it does not modify one isolated file while missing the rest of the contract.
